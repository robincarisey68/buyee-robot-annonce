"""Robot « peluche pokemon fit ».

Deuxième robot, séparé du robot principal (robot.py). Il cherche de la même façon
(Yahoo Auctions + PayPay Flea Market + Mercari, avec liens Buyee), mais il ne
t'envoie une annonce QUE si le prix par peluche est sous la limite (8 € par défaut) :

  1. il cherche les nouvelles annonces pour chaque mot-clé de config_fit.json ;
  2. il trouve le nombre de peluches dans le titre (sinon dans la description) :
     « 3体セット », « まとめ売り 29個 », « 五体 », « ×4 », « 全6種 »… ;
     sans nombre écrit, il compte les noms de Pokémon cités (« ホエルコ ホエルオー » = 2) ;
     s'il ne trouve rien du tout, il n'envoie rien ;
  3. il divise le prix par ce nombre et convertit en euros (taux du jour).
     Seulement les prix fixes : achat immédiat (即決) sur Yahoo, PayPay Flea Market,
     Mercari (pas les enchères) ;
  4. si c'est moins que la limite, il poste l'annonce dans #peluche-pokemon-fit.

Sa mémoire est dans seen_fit.json (le robot principal garde seen.json).
Le lien du salon Discord est lu dans le secret GitHub DISCORD_WEBHOOK_FIT.
"""

import asyncio
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse

import requests

import robot  # on réutilise la recherche et l'envoi Discord du robot principal

ICI = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ICI, "config_fit.json")
MEMOIRE = os.path.join(ICI, "seen_fit.json")
MAX_MEMOIRE = 20000
ALERTE_APRES_ECHECS = 60          # ~3 h de pannes d'affilée -> un message d'alerte
MAX_PREMIER_ENVOI = 10            # bonnes affaires déjà en ligne envoyées au démarrage
MAX_NOMBRE = 300                  # au-delà, on considère que le nombre est une erreur de lecture


# --------------------------------------------------------------------------
# Lire le nombre de peluches dans un texte japonais
# --------------------------------------------------------------------------
_KANJI = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
          "両": 2, "壱": 1, "弐": 2, "参": 3}


def _kanji_vers_nombre(texte):
    """一 -> 1, 十二 -> 12, 二十 -> 20, 三十五 -> 35, 百 -> 100."""
    total, courant = 0, 0
    for c in texte:
        if c in _KANJI:
            courant = _KANJI[c]
        elif c == "十":
            total += (courant or 1) * 10
            courant = 0
        elif c == "百":
            total += (courant or 1) * 100
            courant = 0
        else:
            return None
    return total + courant or None


_NOMBRE = r"(\d{1,3}|[一二三四五六七八九十百両]{1,4})"
# Ce qui suit un nombre et veut dire « tant de peluches ».
_COMPTEUR = r"(?:個|体|點|点|匹|つ|コ|ヶ|ケ|pcs|pieces|piece|items)"
# Ce qui, juste après, montre que ce n'est PAS le nombre de peluches de l'annonce
# (« 2点以上ご購入で… », « 3日以内 », « 1点目 »…).
_PAS_UN_LOT = r"(?!\s*(?:以上|以下|まで|迄|以内|目|ご購入|購入|毎|おまけ|まとめ買い|月|折|口|所|年|日))"
_MOTIFS = [
    re.compile(_NOMBRE + r"\s*" + _COMPTEUR + _PAS_UN_LOT, re.I),       # 3体, 29個, 五体, 10点
    re.compile(r"全\s*" + _NOMBRE + r"\s*種"),                          # 全6種
    re.compile(_NOMBRE + r"\s*種\s*(?:セット|まとめ|コンプ)"),           # 6種セット
    re.compile(r"(?:×|✕|✖|＊|\*|(?<![a-z])x)\s*" + _NOMBRE + r"(?![\d.]|\s*cm)", re.I),  # ×4, x4
    re.compile(r"(?:計|合計|総数|全部で|全)\s*" + _NOMBRE + r"(?!\s*(?:円|日|cm|種))"),  # 計8
    re.compile(_NOMBRE + r"\s*(?:セット|set)(?!\s*(?:以上|目))", re.I),  # 3セット (souvent 3 peluches)
]
_LOT_SANS_NOMBRE = re.compile(r"ペア|2個1組|二個一組")


def nombre_de_peluches(texte):
    """Renvoie le nombre de peluches trouvé dans le texte, ou None si aucun."""
    if not texte:
        return None
    # Chiffres pleine largeur (３) -> chiffres normaux (3), etc.
    t = unicodedata.normalize("NFKC", texte)
    trouves = []
    for motif in _MOTIFS:
        for m in motif.finditer(t):
            brut = m.group(1)
            n = int(brut) if brut.isdigit() else _kanji_vers_nombre(brut)
            if n and 1 <= n <= MAX_NOMBRE:
                trouves.append(n)
    if not trouves and _LOT_SANS_NOMBRE.search(t):
        trouves.append(2)
    return max(trouves) if trouves else None


# Noms japonais des 1025 Pokémon (pokemon_noms.txt). Certains sont aussi des mots
# courants (サンド = sandwich, ブルー = bleu…) : on ne les compte pas.
_MOTS_COURANTS = {"サンド", "クラブ", "ブルー", "ラッキー", "カブト", "プリン", "ボチ", "パモ", "ピィ", "サンダー"}


def _charger_noms():
    try:
        with open(os.path.join(ICI, "pokemon_noms.txt"), encoding="utf-8") as f:
            noms = {l.strip() for l in f if l.strip()} - _MOTS_COURANTS
    except OSError:
        return []
    return sorted(noms, key=len, reverse=True)  # les plus longs d'abord (ミュウツー avant ミュウ)


NOMS_POKEMON = _charger_noms()


def noms_de_pokemon(texte):
    """Liste des Pokémon différents cités dans le texte (ex. ['ホエルコ', 'ホエルオー'])."""
    t = unicodedata.normalize("NFKC", texte or "")
    # Les noms écrits en hiragana (ぴかちゅう) -> katakana (ピカチュウ).
    t = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in t)
    trouves = []
    for nom in NOMS_POKEMON:
        if nom in t:
            trouves.append(nom)
            t = t.replace(nom, "　")  # pour ne pas recompter ミュウ dans ミュウツー
    return trouves


def compter(texte):
    """Nombre de peluches : nombre écrit en priorité, sinon noms de Pokémon cités."""
    n = nombre_de_peluches(texte)
    if n:
        return n, "nombre écrit"
    noms = noms_de_pokemon(texte)
    if noms:
        return len(noms), "noms cités : " + "、".join(noms[:10])
    return None, ""


def parle_de_fit(texte, mots):
    t = unicodedata.normalize("NFKC", texte or "").lower()
    return any(unicodedata.normalize("NFKC", m).lower() in t for m in mots)


def prix_en_yen(annonce):
    chiffres = re.sub(r"\D", "", annonce.get("prix") or "")
    return int(chiffres) if chiffres else None


# --------------------------------------------------------------------------
# Recherche : même méthode que robot.py, mais seulement les prix fixes
# --------------------------------------------------------------------------
def _yen(x):
    return f"{int(x):,} ¥".replace(",", " ")


def chercher_yahoo_prix_fixe(mot_cle):
    """Yahoo Auctions (achat immédiat 即決 seulement) + PayPay Flea Market."""
    url = "https://auctions.yahoo.co.jp/search/search?" + urllib.parse.urlencode(
        {"p": mot_cle, "va": mot_cle, "s1": "new", "o1": "d", "n": 50, "exflg": 1, "b": 1}
    )
    r = requests.get(url, headers=robot.NAVIGATEUR, timeout=30)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("page Yahoo illisible (pas de __NEXT_DATA__)")
    try:
        items = json.loads(m.group(1))["props"]["pageProps"]["initialState"]["search"]["items"]["listing"]["items"]
    except (KeyError, TypeError):
        raise RuntimeError("page Yahoo illisible (liste d'annonces introuvable)")
    resultats = []
    for it in items or []:
        aid = it.get("auctionId")
        if not aid:
            continue
        if it.get("isFleamarketItem"):
            prix = it.get("price") or it.get("buyNowPrice")
            site = "PayPay Flea Market"
            buyee = f"https://buyee.jp/paypayfleamarket/item/{aid}"
            origine = f"https://paypayfleamarket.yahoo.co.jp/item/{aid}"
        else:
            prix = it.get("buyNowPrice")  # pas de prix d'achat immédiat = enchère pure
            site = "Yahoo Auctions (achat immédiat)"
            buyee = f"https://buyee.jp/item/jdirectitems/auction/{aid}"
            origine = f"https://auctions.yahoo.co.jp/jp/auction/{aid}"
        resultats.append({
            "cle": f"yahoo:{aid}",
            "site": site,
            "titre": html.unescape(it.get("title") or f"Annonce {aid}"),
            "prix": _yen(prix) if prix else "",  # vide = ignorée par le robot
            "image": it.get("imageUrl") or "",
            "lien_buyee": buyee,
            "lien_origine": origine,
        })
    return resultats


async def _mercari(mot_cle):
    from mercapi import Mercapi
    from mercapi.requests import SearchRequestData

    res = await Mercapi().search(
        mot_cle,
        sort_by=SearchRequestData.SortBy.SORT_CREATED_TIME,
        sort_order=SearchRequestData.SortOrder.ORDER_DESC,
        status=[SearchRequestData.Status.STATUS_ON_SALE],
    )
    resultats = []
    for it in res.items[:60]:
        enchere = getattr(it, "auction", None)
        if enchere and (getattr(enchere, "id_", None) or getattr(enchere, "bid_deadline", None)):
            prix = None  # enchère Mercari : pas un prix fixe (et pas sur Buyee)
        else:
            prix = getattr(it, "real_price", it.price)
        resultats.append({
            "cle": f"mercari:{it.id_}",
            "site": "Mercari",
            "titre": it.name,
            "prix": _yen(prix) if prix else "",
            "image": it.thumbnails[0] if it.thumbnails else "",
            "lien_buyee": f"https://buyee.jp/mercari/item/{it.id_}",
            "lien_origine": f"https://jp.mercari.com/item/{it.id_}",
        })
    return resultats


def chercher_mercari_prix_fixe(mot_cle):
    return asyncio.run(_mercari(mot_cle))


# --------------------------------------------------------------------------
# Description complète d'une annonce (seulement si le titre ne suffit pas)
# --------------------------------------------------------------------------
def _meta_description(page):
    for nom in ("description", "og:description"):
        m = re.search(
            r'<meta[^>]+(?:name|property)="' + nom + r'"[^>]+content="([^"]*)"', page
        ) or re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:name|property)="' + nom + '"', page)
        if m and m.group(1).strip():
            return html.unescape(m.group(1))
    return ""


async def _description_mercari(identifiant):
    from mercapi import Mercapi

    api = Mercapi()
    if identifiant.startswith("m"):
        item = await api.item(identifiant)
    else:  # boutiques Mercari Shops
        item = await api.product(identifiant)
    return (getattr(item, "description", "") or "") if item else ""


def description(annonce):
    site, ident = annonce["cle"].split(":", 1)
    try:
        if site == "mercari":
            return asyncio.run(_description_mercari(ident))
        if annonce["site"] == "PayPay Flea Market":
            r = requests.get(
                f"https://paypayfleamarket.yahoo.co.jp/api/v1/item/{ident}",
                headers=robot.NAVIGATEUR, timeout=30,
            )
            if r.ok:
                try:
                    return r.json().get("description") or ""
                except ValueError:
                    pass
        r = requests.get(annonce["lien_origine"], headers=robot.NAVIGATEUR, timeout=30)
        r.raise_for_status()
        return _meta_description(r.text)
    except Exception as exc:
        print(f"Description illisible pour {annonce['cle']} : {exc}", file=sys.stderr)
        return ""


# --------------------------------------------------------------------------
# Taux de change yen -> euro
# --------------------------------------------------------------------------
def taux_yen_euro(config, memoire):
    """Taux du jour (Banque centrale européenne), gardé en mémoire 12 h."""
    sauve = memoire.get("taux") or {}
    if sauve.get("valeur") and time.time() - sauve.get("quand", 0) < 12 * 3600:
        return sauve["valeur"], sauve.get("source", "BCE")
    for url, lire in (
        ("https://api.frankfurter.app/latest?from=JPY&to=EUR", lambda d: d["rates"]["EUR"]),
        ("https://open.er-api.com/v6/latest/JPY", lambda d: d["rates"]["EUR"]),
    ):
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            valeur = float(lire(r.json()))
            if 0.001 < valeur < 0.05:  # garde-fou contre un taux absurde
                memoire["taux"] = {"valeur": valeur, "quand": time.time(), "source": "taux du jour"}
                return valeur, "taux du jour"
        except Exception as exc:
            print(f"Taux de change indisponible ({url}) : {exc}", file=sys.stderr)
    if sauve.get("valeur"):
        return sauve["valeur"], "dernier taux connu"
    return float(config["taux_yen_euro_secours"]), "taux de secours"


# --------------------------------------------------------------------------
# Carte Discord
# --------------------------------------------------------------------------
def euros(x):
    return f"{x:.2f} €".replace(".", ",")


def carte(a):
    quantite = f"{a['nombre']} ({a['nombre_source']})"[:1000]
    e = {
        "title": a["titre"][:250],
        "url": a["lien_buyee"],
        "color": 0x57F287,
        "description": f"[Voir sur Buyee]({a['lien_buyee']}) · [Annonce d'origine]({a['lien_origine']})",
        "fields": [
            {"name": "Prix par peluche", "value": f"**{euros(a['euros_par_peluche'])}**", "inline": True},
            {"name": "Peluches", "value": quantite, "inline": True},
            {"name": "Prix total", "value": f"{a['prix']} ≈ {euros(a['euros_total'])}", "inline": True},
            {"name": "Site", "value": a["site"], "inline": True},
            {"name": "Mot-clé", "value": a["mot_cle"], "inline": True},
        ],
    }
    if a["image"]:
        e["thumbnail"] = {"url": a["image"]}
    return e


# --------------------------------------------------------------------------
# Programme principal
# --------------------------------------------------------------------------
def evaluer(annonce, config, taux, lire_description):
    """Calcule le prix par peluche. Renvoie True si c'est une bonne affaire."""
    yen = prix_en_yen(annonce)
    if not yen:
        return False
    limite = float(config["prix_max_par_peluche_euros"])
    if yen * taux / MAX_NOMBRE >= limite:
        return False  # trop cher, même avec un énorme lot
    mots = config["le_titre_doit_contenir"]
    titre_parle_de_fit = parle_de_fit(annonce["titre"], mots)
    if not titre_parle_de_fit and not lire_description:
        return False
    nombre, comment = compter(annonce["titre"])
    source = "le titre"
    # On lit la description si le titre ne dit pas « fit » (pour écarter les cartes,
    # chaussettes… que la recherche remonte aussi), ou si le titre ne donne pas de nombre,
    # ou s'il ne cite qu'un seul Pokémon alors que le prix total dépasse la limite
    # (la description dit peut-être « 5体セット »).
    pas_sur = nombre is None or (nombre == 1 and yen * taux >= limite)
    if lire_description and (not titre_parle_de_fit or pas_sur):
        texte = description(annonce)
        if not titre_parle_de_fit and not parle_de_fit(texte, mots):
            return False
        if pas_sur:
            n2, comment2 = compter(texte)
            if n2 and (nombre is None or n2 > nombre):
                nombre, comment, source = n2, comment2, "la description"
    if not nombre:
        return False  # aucun nombre trouvé : on n'envoie rien
    annonce["nombre"] = nombre
    annonce["nombre_source"] = f"{source}, {comment}"
    annonce["euros_total"] = yen * taux
    annonce["euros_par_peluche"] = yen * taux / nombre
    return annonce["euros_par_peluche"] < limite


def main():
    with open(CONFIG, encoding="utf-8") as f:
        config = json.load(f)
    webhook = os.environ.get(config.get("secret", "DISCORD_WEBHOOK_FIT"), "").strip()
    webhook_general = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    salon = webhook or webhook_general

    premier_passage = not os.path.exists(MEMOIRE)
    memoire = {"vues": [], "recherches_initialisees": [], "echecs_daffilee": 0, "alerte_envoyee": False}
    if not premier_passage:
        with open(MEMOIRE, encoding="utf-8") as f:
            memoire.update(json.load(f))
    deja_vues = set(memoire["vues"])
    initialisees = set(memoire["recherches_initialisees"])
    taux, source_taux = taux_yen_euro(config, memoire)
    print(f"Taux : 1 ¥ = {taux:.6f} € ({source_taux})")

    sources = []
    if config.get("surveiller_yahoo_auctions", True):
        sources.append(("Yahoo Auctions", chercher_yahoo_prix_fixe))
    if config.get("surveiller_mercari", True):
        sources.append(("Mercari", chercher_mercari_prix_fixe))

    affaires, deja_en_ligne, reussites, erreurs, nb_memorisees = [], [], 0, [], 0
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
            recherche = f"{robot.nom_court(nom)}|{mot_cle}"
            nouvelle_recherche = recherche not in initialisees
            initialisees.add(recherche)
            for a in trouvees:
                if a["cle"] in deja_vues:
                    continue
                deja_vues.add(a["cle"])
                memoire["vues"].append(a["cle"])
                a["mot_cle"] = mot_cle
                if nouvelle_recherche:
                    # Annonces déjà en ligne : on ne lit que le titre (pas de description,
                    # pour ne pas faire des centaines de requêtes d'un coup).
                    nb_memorisees += 1
                    if evaluer(a, config, taux, lire_description=False):
                        deja_en_ligne.append(a)
                elif evaluer(a, config, taux, lire_description=True):
                    affaires.append(a)
            time.sleep(1)  # on reste poli avec les sites

    if premier_passage and reussites == 0:
        print("Premier passage impossible, aucun site n'a répondu :", *erreurs, sep="\n", file=sys.stderr)
        sys.exit(1)

    if reussites == 0:
        memoire["echecs_daffilee"] += 1
        if memoire["echecs_daffilee"] >= ALERTE_APRES_ECHECS and not memoire["alerte_envoyee"]:
            robot.envoyer_discord(
                salon,
                "⚠️ Le robot peluches Pokémon fit n'arrive plus à lire les sites depuis environ 3 h. "
                "Il réessaie toutes les 3 min.\n```" + "\n".join(erreurs[:5])[:1500] + "```",
            )
            memoire["alerte_envoyee"] = True
    else:
        if memoire["alerte_envoyee"]:
            robot.envoyer_discord(salon, "✅ Le robot peluches Pokémon fit fonctionne de nouveau.")
        memoire["echecs_daffilee"] = 0
        memoire["alerte_envoyee"] = False

    limite = euros(float(config["prix_max_par_peluche_euros"]))
    if premier_passage:
        robot.envoyer_discord(
            salon,
            f"🧸 Robot peluches Pokémon fit activé ! {nb_memorisees} annonces déjà en ligne ont été lues. "
            f"Je te préviendrai des **nouvelles** annonces à moins de {limite} par peluche "
            "(vérification toutes les 3 min).",
        )
    if deja_en_ligne:
        deja_en_ligne.sort(key=lambda a: a["euros_par_peluche"])
        envoyer_cartes(
            salon,
            deja_en_ligne[:MAX_PREMIER_ENVOI],
            f"🔎 {len(deja_en_ligne)} annonce(s) **déjà en ligne** à moins de {limite} par peluche"
            + (f", voici les {MAX_PREMIER_ENVOI} moins chères :" if len(deja_en_ligne) > MAX_PREMIER_ENVOI else " :"),
        )
    if affaires:
        print(f"{len(affaires)} bonne(s) affaire(s)")
        envoyer_cartes(
            salon,
            affaires[:40],
            "🧸 Bonne affaire Pokémon fit !" if len(affaires) == 1 else f"🧸 {len(affaires)} bonnes affaires Pokémon fit !",
        )
    elif not premier_passage:
        print("Aucune nouvelle bonne affaire.")

    memoire["recherches_initialisees"] = sorted(initialisees)
    memoire["vues"] = memoire["vues"][-MAX_MEMOIRE:]
    with open(MEMOIRE, "w", encoding="utf-8") as f:
        json.dump(memoire, f, ensure_ascii=False, indent=0)


def envoyer_cartes(salon, annonces, entete):
    for i in range(0, len(annonces), 10):  # Discord : 10 cartes max par message
        robot.envoyer_discord(salon, entete if i == 0 else None, [carte(a) for a in annonces[i : i + 10]])


if __name__ == "__main__":
    main()
