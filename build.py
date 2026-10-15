"""Builds site/index.html: a two-tab (Dynasty / Redraft) fantasy dashboard.

Data: nflverse (stats, player IDs, contracts) + Sleeper public API (rosters).
Run locally:  pip install -r requirements.txt && python build.py
"""
import datetime as dt
import json
import os

import pandas as pd
import requests
import nflreadpy as nfl

# ---------- CONFIG (edit these each season) ----------
USERNAME = "belichickIsfat"
LEAGUES = {
    "dynasty": {"id": "1312135390987026432", "label": "Dynasty"},
    "redraft": {"id": "1389103067030388736", "label": "Redraft"},
}
POSITIONS = ["QB", "RB", "WR", "TE"]
# -----------------------------------------------------

SLEEPER = "https://api.sleeper.app/v1"


def get(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def current_season():
    today = dt.date.today()
    return today.year if today.month >= 3 else today.year - 1


def load_stats():
    season = current_season()
    for s in (season, season - 1):  # fall back if the new season has no data yet
        df = nfl.load_player_stats([s]).to_pandas()
        df = df[df["season_type"] == "REG"]
        if len(df):
            return s, df
    raise RuntimeError("No player stats found")


def aggregate(df):
    team_col = "team" if "team" in df.columns else "recent_team"
    for c in ["targets", "receptions", "receiving_yards", "receiving_air_yards",
              "target_share", "air_yards_share", "wopr", "fantasy_points_ppr",
              "carries", "rushing_yards", "receiving_yards_after_catch"]:
        if c not in df.columns:
            df[c] = 0
    df = df[df["position"].isin(POSITIONS)].copy()
    agg = df.groupby("player_id").agg(
        name=("player_display_name", "last"), pos=("position", "last"),
        team=(team_col, "last"), gp=("week", "nunique"),
        tgt=("targets", "sum"), rec=("receptions", "sum"),
        yds=("receiving_yards", "sum"), ay=("receiving_air_yards", "sum"),
        ts=("target_share", "mean"), ays=("air_yards_share", "mean"),
        wopr=("wopr", "mean"), ppr=("fantasy_points_ppr", "sum"),
        car=("carries", "sum"), rush_yds=("rushing_yards", "sum"),
        yac=("receiving_yards_after_catch", "sum"),
    ).reset_index()
    last3 = (df.sort_values("week").groupby("player_id").tail(3)
             .groupby("player_id")["fantasy_points_ppr"].mean().rename("l3"))
    agg = agg.merge(last3, on="player_id", how="left")
    agg["ay_tgt"] = (agg["ay"] / agg["tgt"]).where(agg["tgt"] > 0)
    agg["ppg"] = agg["ppr"] / agg["gp"]
    # Rushing vs. receiving efficiency (blank until there is a minimum sample)
    agg["ypc"] = (agg["rush_yds"] / agg["car"]).where(agg["car"] >= 5)
    agg["yac_rec"] = (agg["yac"] / agg["rec"]).where(agg["rec"] >= 5)
    agg["gap"] = agg["ypc"] - agg["yac_rec"]  # positive = more efficient as a rusher
    return agg


def load_ids():
    ids = nfl.load_ff_playerids().to_pandas()
    keep = [c for c in ["gsis_id", "sleeper_id", "age", "birthdate"] if c in ids.columns]
    ids = ids[keep].dropna(subset=["gsis_id", "sleeper_id"])
    ids["sleeper_id"] = ids["sleeper_id"].apply(lambda x: str(int(float(x))))
    if "age" not in ids.columns and "birthdate" in ids.columns:
        bd = pd.to_datetime(ids["birthdate"], errors="coerce")
        ids["age"] = ((pd.Timestamp.today() - bd).dt.days / 365.25).round(1)
    return ids.drop_duplicates("gsis_id")


def load_contracts(season):
    """Returns gsis_id -> contract end year. Fails soft: contract data is a bonus."""
    try:
        c = nfl.load_contracts().to_pandas()
        c = c[c["is_active"] == True].dropna(subset=["gsis_id", "year_signed", "years"])  # noqa: E712
        c = c.sort_values("year_signed").drop_duplicates("gsis_id", keep="last")
        c["contract_end"] = (c["year_signed"] + c["years"] - 1).astype(int)
        return c[["gsis_id", "contract_end"]]
    except Exception as e:  # noqa: BLE001
        print("Contracts unavailable:", e)
        return pd.DataFrame(columns=["gsis_id", "contract_end"])


def sleeper_owners(league_id, user_id):
    users = get(f"{SLEEPER}/league/{league_id}/users")
    rosters = get(f"{SLEEPER}/league/{league_id}/rosters")
    names = {u["user_id"]: (u.get("metadata") or {}).get("team_name") or u["display_name"]
             for u in users}
    owner = {}
    for r in rosters:
        for p in r.get("players") or []:
            owner[p] = names.get(r["owner_id"], "Unknown")
    return owner, names.get(user_id)


def main():
    season, stats = load_stats()
    print("Season:", season, "rows:", len(stats))
    players = aggregate(stats)
    players = players.merge(load_ids(), left_on="player_id", right_on="gsis_id", how="left")
    players = players.merge(load_contracts(season), on="gsis_id", how="left")
    players["contract_year"] = players["contract_end"] == season

    user_id = get(f"{SLEEPER}/user/{USERNAME}")["user_id"]
    out = {}
    for key, cfg in LEAGUES.items():
        owners, mine = sleeper_owners(cfg["id"], user_id)
        d = players.copy()
        d["owner"] = d["sleeper_id"].map(owners).fillna("Free agent")
        d["mine"] = d["owner"] == mine
        d = d[["name", "pos", "team", "age", "owner", "mine", "contract_end", "contract_year",
               "gp", "tgt", "ts", "ay_tgt", "ays", "wopr", "car", "ypc", "yac_rec", "gap",
               "ppg", "l3"]].round(2)
        out[key] = {"label": cfg["label"], "mine": mine,
                    "rows": json.loads(d.to_json(orient="records"))}

    payload = {"season": season, "updated": dt.datetime.utcnow().strftime("%b %d, %Y %H:%M UTC"),
               "leagues": out}
    with open("template.html", encoding="utf-8") as f:
        html = f.read().replace("__DATA__", json.dumps(payload))
    os.makedirs("site", exist_ok=True)
    with open("site/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Wrote site/index.html")


if __name__ == "__main__":
    main()
