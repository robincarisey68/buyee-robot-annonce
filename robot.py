"""Robot d'alerte Buyee.

A chaque passage (toutes les 30 min via GitHub Actions) :
  1. cherche les annonces les plus récentes pour chaque mot-clé de config.json
     (Yahoo Auctions Japon, et Mercari Japon) ;
  2. garde seulement celles jamais vues (mémoire dans seen.json) ;
  3. envoie chaque nouvelle annonce sur Discord, avec le lien Buyee.

Le lien du webhook Discord est lu dans la variable d'environnement
DISCORD_WEBHOOK_URL (secret GitHub), jamais écrit dans le code.
"""

import asyncio
import html
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

ICI = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ICI, "config.json")
MEMOIRE = os.path.join(ICI, "seen.json")
MAX_MEMOIRE = 20000          # nombre max d'annonces gardées en mémoire
ALERTE_APRES_ECHECS = 6      # 6 passages ratés d'affilée = 3 h -> message d'alerte

NAVIGATEUR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
}


# --------------------------------------------------------------------------
# Recherche Yahoo Auctions (c'est ce que Buyee affiche pour les enchères)
# --------------------------------------------------------------------------
def chercher_yahoo(mot_cle):
    url = (
        "https://auctions.yahoo.co.jp/search/search?"
        + urllib.parse.urlencode(
            {"p": mot_cle, "va": mot_cle, "s1": "new", "o1": "d", "n": 50, "exflg": 1, "b": 1}
        )
    )
    r = requests.get(url, headers=NAVIGATEUR, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    annonces = {}
    for a in soup.select("a[href*='/jp/auction/'], a[data-auction-id]"):
        m = re.search(r"/jp/auction/([A-Za-z0-9]+)", a.get("href", ""))
        aid = a.get("data-auction-id") or (m.group(1) if m else None)
        if not aid:
            continue
        info = annonces.setdefault(aid, {"titre": "", "prix": "", "image": ""})
        titre = (
            a.get("data-auction-title")
            or a.get("title")
            or a.get_text(" ", strip=True)
            or (a.img.get("alt", "") if a.img else "")
        )
        if titre and len(titre) > len(info["titre"]):
            info["titre"] = titre
        if a.get("data-auction-price") and not info["prix"]:
            info["prix"] = a["data-auction-price"]
        img = a.get("data-auction-img") or (a.img.get("src", "") if a.img else "")
        if img.startswith("http") and not info["image"]:
            info["image"] = img

    resultats = []
    for aid, info in annonces.items():
        prix = info["prix"]
        resultats.append(
            {
                "cle": f"yahoo:{aid}",
                "site": "Yahoo Auctions",
                "titre": html.unescape(info["titre"]) or f"Enchère {aid}",
                "prix": f"{int(prix):,} ¥".replace(",", " ") if str(prix).isdigit() else "",
                "image": info["image"],
                "lien_buyee": f"https://buyee.jp/item/yahoo/auction/{aid}",
                "lien_origine": f"https://auctions.yahoo.co.jp/jp/auction/{aid}",
            }
        )
    return resultats


# --------------------------------------------------------------------------
# Recherche Mercari (Buyee propose aussi Mercari)
# --------------------------------------------------------------------------
async def _mercari(mot_cle):
    from mercapi import Mercapi
    from mercapi.requests import SearchRequestData

    api = Mercapi()
    res = await api.search(
        mot_cle,
        sort_by=SearchRequestData.SortBy.SORT_CREATED_TIME,
        sort_order=SearchRequestData.SortOrder.ORDER_DESC,
        status=[SearchRequestData.Status.STATUS_ON_SALE],
    )
    resultats = []
    for it in res.items[:60]:
        prix = getattr(it, "real_price", it.price)
        resultats.append(
            {
                "cle": f"mercari:{it.id_}",
                "site": "Mercari",
                "titre": it.name,
                "prix": f"{prix:,} ¥".replace(",", " ") if prix else "",
                "image": it.thumbnails[0] if it.thumbnails else "",
                "lien_buyee": f"https://buyee.jp/mercari/item/{it.id_}",
                "lien_origine": f"https://jp.mercari.com/item/{it.id_}",
            }
        )
    return resultats


def chercher_mercari(mot_cle):
    return asyncio.run(_mercari(mot_cle))


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------
def envoyer_discord(webhook, contenu=None, embeds=None):
    if not webhook:
        print("[pas de webhook]", contenu or "", [e.get("title") for e in embeds or []])
        return
    for essai in range(5):
        r = requests.post(webhook, json={"content": contenu, "embeds": embeds or []}, timeout=30)
        if r.status_code == 429:  # trop de messages : Discord demande d'attendre
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        time.sleep(1)
        return


def carte(annonce):
    e = {
        "title": annonce["titre"][:250],
        "url": annonce["lien_buyee"],
        "color": 0xF7C600 if annonce["site"] == "Yahoo Auctions" else 0xFF4F50,
        "fields": [
            {"name": "Site", "value": annonce["site"], "inline": True},
            {"name": "Mot-clé", "value": annonce["mot_cle"], "inline": True},
        ],
        "description": f"[Voir sur Buyee]({annonce['lien_buyee']}) · [Annonce d'origine]({annonce['lien_origine']})",
    }
    if annonce["prix"]:
        e["fields"].insert(0, {"name": "Prix", "value": annonce["prix"], "inline": True})
    if annonce["image"]:
        e["thumbnail"] = {"url": annonce["image"]}
    return e


# --------------------------------------------------------------------------
# Programme principal
# --------------------------------------------------------------------------
def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    with open(CONFIG, encoding="utf-8") as f:
        config = json.load(f)

    premier_passage = not os.path.exists(MEMOIRE)
    memoire = {"vues": [], "echecs_daffilee": 0, "alerte_envoyee": False}
    if not premier_passage:
        with open(MEMOIRE, encoding="utf-8") as f:
            memoire.update(json.load(f))
    deja_vues = set(memoire["vues"])

    sources = []
    if config.get("surveiller_yahoo_auctions", True):
        sources.append(("Yahoo Auctions", chercher_yahoo))
    if config.get("surveiller_mercari", True):
        sources.append(("Mercari", chercher_mercari))

    nouvelles, reussites, erreurs = [], 0, []
    for mot_cle in config["mots_cles"]:
        for nom, fonction in sources:
            try:
                trouvees = fonction(mot_cle)
                reussites += 1
                print(f"{nom} | {mot_cle} : {len(trouvees)} annonces")
            except Exception as exc:  # un site en panne ne bloque pas les autres
                erreurs.append(f"{nom} / {mot_cle} : {exc}")
                print(f"ERREUR {nom} | {mot_cle} : {exc}", file=sys.stderr)
                continue
            for a in trouvees:
                if a["cle"] not in deja_vues:
                    deja_vues.add(a["cle"])
                    a["mot_cle"] = mot_cle
                    nouvelles.append(a)
                    memoire["vues"].append(a["cle"])
            time.sleep(1)  # on reste poli avec les sites

    if premier_passage and reussites == 0:
        print("Premier passage impossible, aucun site n'a répondu :", *erreurs, sep="\n", file=sys.stderr)
        sys.exit(1)

    # Suivi des pannes : si plus rien ne marche pendant 3 h, on prévient une fois.
    if reussites == 0:
        memoire["echecs_daffilee"] += 1
        if memoire["echecs_daffilee"] >= ALERTE_APRES_ECHECS and not memoire["alerte_envoyee"]:
            envoyer_discord(
                webhook,
                "⚠️ Le robot Buyee n'arrive plus à lire les sites depuis environ 3 h. "
                "Il réessaie toutes les 30 min.\n```" + "\n".join(erreurs[:5])[:1500] + "```",
            )
            memoire["alerte_envoyee"] = True
    else:
        if memoire["alerte_envoyee"]:
            envoyer_discord(webhook, "✅ Le robot Buyee fonctionne de nouveau.")
        memoire["echecs_daffilee"] = 0
        memoire["alerte_envoyee"] = False

    if premier_passage:
        # Premier lancement : on mémorise ce qui existe déjà sans tout envoyer.
        envoyer_discord(
            webhook,
            f"🤖 Robot Buyee activé ! {len(nouvelles)} annonces déjà en ligne ont été mémorisées. "
            "Je te préviendrai uniquement des **nouvelles** annonces, toutes les 30 min.",
        )
    elif nouvelles:
        print(f"{len(nouvelles)} nouvelle(s) annonce(s)")
        if len(nouvelles) > 40:
            envoyer_discord(webhook, f"📦 {len(nouvelles)} nouvelles annonces d'un coup, voici les 40 plus récentes :")
            nouvelles = nouvelles[:40]
        for i in range(0, len(nouvelles), 10):  # Discord : 10 cartes max par message
            paquet = nouvelles[i : i + 10]
            entete = "🔔 Nouvelle annonce !" if len(nouvelles) == 1 else None
            envoyer_discord(webhook, entete if i == 0 else None, [carte(a) for a in paquet])
    else:
        print("Aucune nouvelle annonce.")

    memoire["vues"] = memoire["vues"][-MAX_MEMOIRE:]
    memoire["dernier_passage"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(MEMOIRE, "w", encoding="utf-8") as f:
        json.dump(memoire, f, ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
