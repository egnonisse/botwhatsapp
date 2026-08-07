"""Recherche sémantique par dictionnaire de synonymes + règles.

Instant (<1ms), gratuit, sans LLM.
Transforme "un truc pour écouter la musique" → "ecouteurs casque audio enceinte bluetooth"
"""

import re

# Dictionnaire de synonymes orienté e-commerce
SYNONYMS = {
    # Écoute / Audio
    "écouter": "ecouteurs casque enceinte audio bluetooth",
    "ecouter": "ecouteurs casque enceinte audio bluetooth",
    "musique": "ecouteurs casque enceinte audio bluetooth",
    "son": "enceinte barre son audio casque ecouteurs",
    "audio": "enceinte barre son casque ecouteurs bluetooth",

    # TV / Écran
    "film": "tv television ecran smart tv 4K",
    "films": "tv television ecran smart tv 4K",
    "tv": "television ecran smart tv 4K led qled",
    "télé": "television ecran smart tv 4K led qled",
    "télévision": "television ecran smart tv 4K led qled",
    "écran": "ecran tv television moniteur tablette smartphone",
    "ecran": "ecran tv television moniteur tablette smartphone",
    "regarder": "tv television ecran smartphone tablette",
    "mater": "tv television ecran smartphone tablette",
    "cinéma": "tv television ecran barre son home cinema",
    "cinema": "tv television ecran barre son home cinema",

    # Photo
    "photo": "smartphone appareil photo camera 50MP 108MP",
    "photos": "smartphone appareil photo camera 50MP 108MP",
    "belles photos": "smartphone 50MP 108MP camera photo",
    "photographie": "smartphone appareil photo camera",

    # Batterie / Autonomie
    "batterie": "smartphone batterie 5000mAh powerbank chargeur",
    "autonomie": "smartphone batterie 5000mAh longue autonomie",
    "charge": "chargeur cable batterie powerbank",
    "recharge": "chargeur cable batterie powerbank",

    # Repassage
    "repasser": "fer a repasser defroisseur vapeur",
    "repassage": "fer a repasser defroisseur vapeur",
    "vêtements": "fer a repasser defroisseur machine a laver",
    "vetements": "fer a repasser defroisseur machine a laver",

    # Ménage
    "aspirateur": "aspirateur balai robot traineau sans fil",
    "nettoyer": "aspirateur balai robot nettoyeur vapeur",
    "nettoyage": "aspirateur balai robot nettoyeur vapeur",
    "poussière": "aspirateur balai robot nettoyeur",
    "poussiere": "aspirateur balai robot nettoyeur",

    # Froid
    "frigo": "refrigerateur congelateur frigo refroidisseur",
    "réfrigérateur": "refrigerateur congelateur frigo refroidisseur",
    "refrigerateur": "refrigerateur congelateur frigo refroidisseur",
    "congélateur": "congelateur refrigerateur frigo froid",
    "congelateur": "congelateur refrigerateur frigo froid",
    "frais": "refrigerateur congelateur climatiseur ventilateur",

    # Cuisine
    "cuisiner": "cuisiniere four micro-ondes mixeur blender",
    "cuisine": "cuisiniere four micro-ondes mixeur blender refrigerateur",
    "four": "four micro-ondes cuisiniere",
    "repas": "cuisiniere four micro-ondes mixeur",

    # Santé / Sport
    "sport": "montre connectee smartwatch ecouteurs sport bracelet",
    "courir": "ecouteurs sport montre connectee smartwatch",
    "course": "ecouteurs sport montre connectee smartwatch",
    "santé": "montre connectee smartwatch purificateur humidificateur",
    "sante": "montre connectee smartwatch purificateur humidificateur",

    # Informatique
    "ordinateur": "ordinateur portable pc laptop mini pc",
    "pc": "ordinateur portable pc laptop mini pc",
    "travailler": "ordinateur portable pc laptop bureau",
    "bureau": "ordinateur portable pc laptop souris clavier bureau",
    "jeux": "smartphone gaming ordinateur portable pc ecouteurs gaming",
    "gaming": "smartphone gaming ordinateur portable ecouteurs",

    # Téléphone générique
    "téléphone": "smartphone telephone mobile portable android iphone",
    "telephone": "smartphone telephone mobile portable android iphone",
    "portable": "smartphone telephone mobile ordinateur portable",
    "mobile": "smartphone telephone android iphone",

    # Beau / Design
    "beau": "smartphone design elegant premium",
    "belle": "smartphone design elegant premium",
    "joli": "smartphone design elegant premium",
    "stylé": "smartphone design elegant premium",
    "style": "smartphone design elegant premium",

    # Pas cher / Budget
    "pas cher": "budget economique entree de gamme promotion",
    "abordable": "budget economique entree de gamme promotion",
    "économique": "budget economique entree de gamme",
    "economique": "budget economique entree de gamme",
    "bon marché": "budget economique entree de gamme",
    "bon marche": "budget economique entree de gamme",

    # Lavage
    "laver": "machine a laver seche linge lave linge",
    "linge": "machine a laver seche linge lave linge",
    "sèche": "seche linge machine a laver",
    "seche": "seche linge machine a laver",
}


def expand_query(query: str) -> str:
    """Transforme une requête naturelle en mots-clés enrichis par synonymes.

    "un truc pour écouter la musique" → "truc ecouter musique ecouteurs casque enceinte audio bluetooth"
    """
    query_lower = query.lower()
    words = query_lower.split()
    expanded = []

    for word in words:
        word_clean = re.sub(r'[^\w]', '', word)
        if word_clean in SYNONYMS:
            expanded.append(SYNONYMS[word_clean])
        else:
            expanded.append(word)

    result = " ".join(expanded)

    # Ajouter aussi les synonymes pour les bigrammes (2 mots)
    for phrase, synonyms in SYNONYMS.items():
        if " " in phrase and phrase in query_lower:
            result += " " + synonyms

    return result[:300]


def search_semantic(cache, query: str, limit: int = 8, max_price: float = None):
    """Recherche sémantique complète : expansion + FTS5.

    Returns (products, was_expanded: bool)
    """
    # 1. Chercher directement
    results = cache.search(query, limit=limit, max_price=max_price)

    # 2. Si peu de résultats, tenter l'expansion
    if len(results) < 3:
        expanded = expand_query(query)
        results = cache.search(expanded, limit=limit, max_price=max_price)
        return results, True

    return results, False
