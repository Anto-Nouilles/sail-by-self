#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Générateur de pronostics — Sail by Self
========================================
Récupère les résultats et calendriers des 5 grands championnats via
l'API football-data.org, calcule deux modèles de prédiction (Poisson
et Elo), puis écrit le fichier ../data.js lu par la page beta.html.

Utilisation :
    python3 predict.py            # mode réel (nécessite une clé API)
    python3 predict.py --demo     # mode démo (données fictives, sans clé)
    python3 predict.py --fetch PL # récupère un seul championnat (cache)
    python3 predict.py --build    # construit data.js depuis le cache

Clé API (gratuite) :
    1. S'inscrire sur https://www.football-data.org/client/register
    2. Coller la clé dans le fichier api_key.txt à côté de ce script
       (ou définir la variable d'environnement FOOTBALL_DATA_KEY)

Aucune dépendance externe : uniquement la bibliothèque standard Python.
"""

import json
import math
import os
import random
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
API_BASE = "https://api.football-data.org/v4"
COMPETITIONS = {
    "FL1": "Ligue 1",
    "PL":  "Premier League",
    "PD":  "Liga",
    "SA":  "Serie A",
    "BL1": "Bundesliga",
}
API_DELAY = 7            # secondes entre deux appels (limite : 10 appels/min)
DAYS_AHEAD = 14          # fenêtre des matchs à venir
HISTORY_DAYS = 21        # fenêtre de l'historique affiché
MIN_MATCHES = 4          # matchs minimum par équipe avant de prédire
ELO_START = 1500.0
ELO_K = 20.0
ELO_HOME_ADV = 60.0      # avantage domicile en points Elo
MAX_GOALS = 8            # taille de la matrice de scores (Poisson)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "..", "data.js")
CACHE_DIR = os.path.join(SCRIPT_DIR, "cache")


# ----------------------------------------------------------------------
# Accès API
# ----------------------------------------------------------------------
def get_api_key():
    key = os.environ.get("FOOTBALL_DATA_KEY", "").strip()
    if key:
        return key
    key_file = os.path.join(SCRIPT_DIR, "api_key.txt")
    if os.path.exists(key_file):
        with open(key_file, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def api_get(path, key):
    req = urllib.request.Request(API_BASE + path, headers={"X-Auth-Token": key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def current_season_year(today):
    """Saison européenne : commence en août → l'année de saison est
    l'année civile si on est après juillet, sinon l'année précédente."""
    return today.year if today.month >= 7 else today.year - 1


def fetch_league(code, key, today):
    """Retourne (matchs_terminés, matchs_à_venir) triés par date."""
    season = current_season_year(today)
    finished, upcoming = [], []

    data = api_get(f"/competitions/{code}/matches?season={season}&status=FINISHED", key)
    finished = data.get("matches", [])
    # Début de saison : la saison N n'a pas encore de résultats,
    # on se rabat sur la saison précédente pour calibrer les modèles.
    if len(finished) < 30:
        time.sleep(API_DELAY)
        data = api_get(f"/competitions/{code}/matches?season={season - 1}&status=FINISHED", key)
        finished = data.get("matches", []) + finished

    time.sleep(API_DELAY)
    d1 = today.strftime("%Y-%m-%d")
    d2 = (today + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")
    data = api_get(f"/competitions/{code}/matches?dateFrom={d1}&dateTo={d2}&status=SCHEDULED", key)
    upcoming = data.get("matches", [])

    def norm(m, played):
        score = m.get("score", {}).get("fullTime", {})
        return {
            "date": m["utcDate"][:10],
            "home": m["homeTeam"]["shortName"] or m["homeTeam"]["name"],
            "away": m["awayTeam"]["shortName"] or m["awayTeam"]["name"],
            "hg": score.get("home") if played else None,
            "ag": score.get("away") if played else None,
        }

    finished = sorted((norm(m, True) for m in finished), key=lambda x: x["date"])
    upcoming = sorted((norm(m, False) for m in upcoming), key=lambda x: x["date"])
    finished = [m for m in finished if m["hg"] is not None and m["ag"] is not None]
    return finished, upcoming


# ----------------------------------------------------------------------
# Mode démo : championnats fictifs pour tester sans clé API
# ----------------------------------------------------------------------
DEMO_TEAMS = ["FC Rivière", "Étoile du Nord", "AS Colline", "Union Portuaire",
              "Racing Lacustre", "Sporting Vallée", "Olympique des Dunes", "Real Falaise"]


def demo_league(rng, today):
    """Simule une demi-saison + 2 journées à venir pour 8 équipes."""
    strengths = {t: rng.uniform(0.7, 1.5) for t in DEMO_TEAMS}
    finished, upcoming = [], []
    day = today - timedelta(days=90)
    matchups = [(h, a) for h in DEMO_TEAMS for a in DEMO_TEAMS if h != a]
    rng.shuffle(matchups)
    for i, (h, a) in enumerate(matchups[:40]):
        lam_h = 1.45 * strengths[h] / strengths[a]
        lam_a = 1.15 * strengths[a] / strengths[h]
        finished.append({
            "date": (day + timedelta(days=i * 2)).strftime("%Y-%m-%d"),
            "home": h, "away": a,
            "hg": poisson_sample(rng, lam_h), "ag": poisson_sample(rng, lam_a),
        })
    for j, (h, a) in enumerate(matchups[40:48]):
        upcoming.append({
            "date": (today + timedelta(days=1 + j // 4 * 3)).strftime("%Y-%m-%d"),
            "home": h, "away": a, "hg": None, "ag": None,
        })
    return finished, upcoming


def poisson_sample(rng, lam):
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


# ----------------------------------------------------------------------
# Modèle 1 : Poisson (attaque/défense incrémentales)
# ----------------------------------------------------------------------
class PoissonModel:
    """Buts attendus par équipe, mis à jour match après match.
    λ_dom = moy_dom × attaque_dom × défense_ext ; idem à l'extérieur."""

    def __init__(self):
        self.gf = {}; self.ga = {}; self.n = {}
        self.total_goals = 0.0; self.total_matches = 0
        self.home_goals = 0.0; self.away_goals = 0.0

    def ready(self, home, away):
        return self.n.get(home, 0) >= MIN_MATCHES and self.n.get(away, 0) >= MIN_MATCHES

    def predict(self, home, away):
        avg = self.total_goals / (2 * self.total_matches)
        avg_h = self.home_goals / self.total_matches
        avg_a = self.away_goals / self.total_matches
        att_h = (self.gf[home] / self.n[home]) / avg
        def_h = (self.ga[home] / self.n[home]) / avg
        att_a = (self.gf[away] / self.n[away]) / avg
        def_a = (self.ga[away] / self.n[away]) / avg
        lam_h = max(0.05, avg_h * att_h * def_a)
        lam_a = max(0.05, avg_a * att_a * def_h)

        p_home = p_draw = p_away = p_over25 = 0.0
        for i in range(MAX_GOALS + 1):
            pi = math.exp(-lam_h) * lam_h ** i / math.factorial(i)
            for j in range(MAX_GOALS + 1):
                pj = math.exp(-lam_a) * lam_a ** j / math.factorial(j)
                p = pi * pj
                if i > j: p_home += p
                elif i == j: p_draw += p
                else: p_away += p
                if i + j > 2.5: p_over25 += p
        s = p_home + p_draw + p_away
        return {"home": p_home / s, "draw": p_draw / s, "away": p_away / s,
                "over25": p_over25 / s, "xg": (round(lam_h, 2), round(lam_a, 2))}

    def update(self, m):
        for team, gf, ga in ((m["home"], m["hg"], m["ag"]), (m["away"], m["ag"], m["hg"])):
            self.gf[team] = self.gf.get(team, 0) + gf
            self.ga[team] = self.ga.get(team, 0) + ga
            self.n[team] = self.n.get(team, 0) + 1
        self.total_goals += m["hg"] + m["ag"]
        self.total_matches += 1
        self.home_goals += m["hg"]
        self.away_goals += m["ag"]


# ----------------------------------------------------------------------
# Modèle 2 : Elo
# ----------------------------------------------------------------------
class EloModel:
    """Note de niveau par équipe, avantage domicile inclus.
    Le nul est estimé par une heuristique décroissante avec l'écart de niveau."""

    def __init__(self):
        self.rating = {}; self.n = {}

    def ready(self, home, away):
        return self.n.get(home, 0) >= MIN_MATCHES and self.n.get(away, 0) >= MIN_MATCHES

    def predict(self, home, away):
        rh = self.rating.get(home, ELO_START) + ELO_HOME_ADV
        ra = self.rating.get(away, ELO_START)
        expected = 1.0 / (1.0 + 10 ** ((ra - rh) / 400.0))
        p_draw = max(0.10, 0.32 - 0.35 * abs(expected - 0.5))
        p_home = max(0.02, expected - p_draw / 2)
        p_away = max(0.02, 1.0 - p_home - p_draw)
        s = p_home + p_draw + p_away
        return {"home": p_home / s, "draw": p_draw / s, "away": p_away / s,
                "over25": None, "elo": (round(rh - ELO_HOME_ADV), round(ra))}

    def update(self, m):
        rh = self.rating.get(m["home"], ELO_START)
        ra = self.rating.get(m["away"], ELO_START)
        expected = 1.0 / (1.0 + 10 ** ((ra - rh - ELO_HOME_ADV) / 400.0))
        result = 1.0 if m["hg"] > m["ag"] else 0.0 if m["hg"] < m["ag"] else 0.5
        margin = 1.0 + math.log1p(abs(m["hg"] - m["ag"]))
        delta = ELO_K * margin * (result - expected)
        self.rating[m["home"]] = rh + delta
        self.rating[m["away"]] = ra - delta
        self.n[m["home"]] = self.n.get(m["home"], 0) + 1
        self.n[m["away"]] = self.n.get(m["away"], 0) + 1


# ----------------------------------------------------------------------
# Génération des pronostics
# ----------------------------------------------------------------------
def tip_from_probs(probs, home, away):
    best = max(("home", "draw", "away"), key=lambda k: probs[k])
    labels = {"home": f"Victoire {home}", "draw": "Match nul", "away": f"Victoire {away}"}
    return best, labels[best], round(probs[best] * 100)


def fair_odds(p):
    return round(1.0 / max(p, 0.01), 2)


def process_league(code, name, finished, upcoming, today):
    """Rejoue la saison chronologiquement : chaque match passé est prédit
    avec les seules données antérieures (backtest honnête), puis les
    matchs à venir sont prédits avec les modèles à jour."""
    models = {"poisson": PoissonModel(), "elo": EloModel()}
    picks = []
    history_cutoff = (today - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    # Intersaison : garantit au moins les ~10 derniers matchs dans l'historique
    if finished:
        tail_cutoff = finished[max(0, len(finished) - 10)]["date"]
        history_cutoff = min(history_cutoff, tail_cutoff)

    for m in finished:
        for model_name, model in models.items():
            if model.ready(m["home"], m["away"]) and m["date"] >= history_cutoff:
                probs = model.predict(m["home"], m["away"])
                key, label, conf = tip_from_probs(probs, m["home"], m["away"])
                actual = "home" if m["hg"] > m["ag"] else "away" if m["hg"] < m["ag"] else "draw"
                picks.append({
                    "date": m["date"], "league": code, "leagueName": name,
                    "match": f'{m["home"]} — {m["away"]}',
                    "model": model_name, "tip": label, "confidence": conf,
                    "odds": fair_odds(probs[key]),
                    "score": f'{m["hg"]}–{m["ag"]}',
                    "status": "win" if key == actual else "loss",
                })
        for model in models.values():
            model.update(m)

    for m in upcoming:
        for model_name, model in models.items():
            if model.ready(m["home"], m["away"]):
                probs = model.predict(m["home"], m["away"])
                key, label, conf = tip_from_probs(probs, m["home"], m["away"])
                picks.append({
                    "date": m["date"], "league": code, "leagueName": name,
                    "match": f'{m["home"]} — {m["away"]}',
                    "model": model_name, "tip": label, "confidence": conf,
                    "odds": fair_odds(probs[key]),
                    "score": None, "status": "pending",
                })
    return picks


def save_cache(code, finished, upcoming):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, code + ".json"), "w", encoding="utf-8") as f:
        json.dump({"finished": finished, "upcoming": upcoming}, f, ensure_ascii=False)


def load_cache(code):
    path = os.path.join(CACHE_DIR, code + ".json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return d["finished"], d["upcoming"]


def write_output(all_picks, leagues, today, demo):
    all_picks.sort(key=lambda p: (p["status"] != "pending", p["date"]))
    payload = {
        "generatedAt": today.strftime("%Y-%m-%d %H:%M UTC"),
        "demo": demo,
        "leagues": leagues,
        "picks": all_picks,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write("// Fichier généré par generator/predict.py — ne pas éditer à la main.\n")
        f.write("window.PRONOSTICS_DATA = ")
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write(";\n")
    pending = sum(1 for p in all_picks if p["status"] == "pending")
    print(f"\n✓ {len(all_picks)} pronostics écrits dans data.js "
          f"({pending} à venir, {len(all_picks) - pending} en historique).")
    if not demo and pending == 0:
        print("  (Aucun match à venir sous 14 jours — normal en intersaison.)")


def main():
    demo = "--demo" in sys.argv
    today = datetime.now(timezone.utc)
    all_picks = []
    leagues = []

    # --fetch CODE : récupère un seul championnat vers le cache puis s'arrête
    if "--fetch" in sys.argv:
        code = sys.argv[sys.argv.index("--fetch") + 1].upper()
        if code not in COMPETITIONS:
            sys.exit(f"Championnat inconnu : {code} (choix : {', '.join(COMPETITIONS)})")
        key = get_api_key()
        if not key:
            sys.exit("Clé API manquante (generator/api_key.txt).")
        finished, upcoming = fetch_league(code, key, today)
        save_cache(code, finished, upcoming)
        print(f"✓ {COMPETITIONS[code]} : {len(finished)} matchs joués · "
              f"{len(upcoming)} à venir → cache/{code}.json")
        return

    # --build : construit data.js à partir des caches présents
    if "--build" in sys.argv:
        for code, name in COMPETITIONS.items():
            cached = load_cache(code)
            if cached is None:
                print(f"  ! pas de cache pour {name} — ignoré")
                continue
            finished, upcoming = cached
            all_picks += process_league(code, name, finished, upcoming, today)
            leagues.append({"code": code, "name": name})
        write_output(all_picks, leagues, today, False)
        return

    if demo:
        print("Mode démo : génération de données fictives (aucun appel API).")
        rng = random.Random(42)
        for i, (code, name) in enumerate(COMPETITIONS.items()):
            finished, upcoming = demo_league(rng, today)
            all_picks += process_league(code, f"{name} (démo)", finished, upcoming, today)
            leagues.append({"code": code, "name": f"{name} (démo)"})
    else:
        key = get_api_key()
        if not key:
            sys.exit(
                "Aucune clé API trouvée.\n"
                "→ Inscris-toi (gratuit) : https://www.football-data.org/client/register\n"
                "→ Colle la clé dans generator/api_key.txt\n"
                "→ Ou lance :  python3 predict.py --demo  pour tester sans clé."
            )
        for i, (code, name) in enumerate(COMPETITIONS.items()):
            print(f"[{i + 1}/{len(COMPETITIONS)}] {name}…")
            try:
                finished, upcoming = fetch_league(code, key, today)
            except Exception as exc:
                print(f"  ! {name} ignorée ({exc})")
                continue
            print(f"  {len(finished)} matchs joués · {len(upcoming)} à venir")
            save_cache(code, finished, upcoming)
            all_picks += process_league(code, name, finished, upcoming, today)
            leagues.append({"code": code, "name": name})
            if i < len(COMPETITIONS) - 1:
                time.sleep(API_DELAY)

    write_output(all_picks, leagues, today, demo)


if __name__ == "__main__":
    main()
