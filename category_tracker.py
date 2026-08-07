"""Détection de catégorie et suivi du sujet dans la conversation.

Répond à deux besoins produits :
  1. Savoir QUAND le client change de catégorie (TV -> téléphone -> électroménager)
     afin de ne pas continuer à recommander l'ancienne famille de produits.
  2. Fournir une requête catalogue exploitable même quand le client répond
     "peu importe" / "oui" — messages qui ne contiennent aucun mot-clé produit.

Volontairement déterministe (regex + mots-clés) : aucun appel LLM, latence < 1 ms.
"""

import re
import unicodedata

# Familles de produits -> mots-clés client (langage réel, pas les noms de catégories WooCommerce)
CATEGORY_KEYWORDS = {
    "tv": [
        "tv", "tele", "television", "televiseur", "ecran plat", "smart tv",
        "qled", "oled", "uled", "dled", "miniled", "pouces",
    ],
    "smartphone": [
        "telephone", "portable", "smartphone", "mobile", "android", "iphone",
        "samsung", "tecno", "xiaomi", "infinix", "itel", "redmi", "blackview",
        "zte", "oppo", "realme",
    ],
    "tablette": ["tablette", "tablet", "ipad", "galaxy tab"],
    "audio": [
        "barre de son", "barre son", "soundbar", "enceinte", "haut parleur",
        "hifi", "hi fi", "casque", "ecouteur", "airpods", "audio", "musique",
    ],
    "froid": [
        "frigo", "refrigerateur", "congelateur", "congele", "refroidisseur",
        "vitrine", "glaciere",
    ],
    "climatisation": [
        "clim", "climatiseur", "climatisation", "split", "ventilateur",
        "brasseur", "purificateur",
    ],
    "lavage": [
        "machine a laver", "machine laver", "lave linge", "laveuse",
        "seche linge", "lave vaisselle",
    ],
    "aspirateur": [
        "aspirateur", "aspirateur balai", "aspirateur sans fil",
        "aspirateur traineau", "aspirateur filaire", "aspirateur rechargeable",
        "cybervac", "aspirateur main", "balai electrique", "cleaner",
        "nettoyage sol", "sans sac", "aspirateur anti acarien",
    ],
    "cuisine": [
        "cuisiniere", "four", "micro onde", "micro-onde", "plaque", "rechaud",
        "mixeur", "blender", "robot", "friteuse", "bouilloire", "cafetiere",
        # Besoins de cuisine : "quelque chose pour cuisiner", "pour manger"
        "cuisiner", "manger", "repas", "cuisine", "aliment", "gouter", "dejeuner",
    ],
    "beaute": [
        # Produits beauté / soin (le catalogue en a : épilateur, rasoir, tondeuse...)
        "epilateur", "rasoir", "tondeuse", "brosse", "lisseur", "seche cheveux",
        "maquillage", "parfum", "soin", "beaute", "beauté", "coiffure",
        # Besoins : cadeau pour femme / homme -> montrer les produits beauté
        "cadeau", "offrir", "femme", "homme", "anniversaire",
    ],
    "informatique": [
        "ordinateur", "pc", "laptop", "portable pc", "imprimante", "onduleur",
        "clavier", "souris", "disque dur", "cle usb",
    ],
    "accessoire": [
        "chargeur", "cable", "coque", "etui", "protection", "verre trempe",
        "powerbank", "power bank", "batterie externe", "support",
    ],
}

# Famille -> requête FTS5 utilisée quand le client ne donne aucun critère
CATEGORY_QUERY = {
    "tv": "TV",
    "smartphone": "smartphone telephone",
    "tablette": "tablette",
    "audio": "barre de son enceinte",
    "froid": "refrigerateur congelateur",
    "climatisation": "climatiseur ventilateur",
    "lavage": "machine a laver",
    "aspirateur": "aspirateur",
    "cuisine": "cuisiniere four mixeur",
    "beaute": "rasoir tondeuse epilateur brosse",
    "informatique": "ordinateur imprimante",
    "accessoire": "chargeur coque cable",
}

# Réponses qui n'apportent aucun critère : le client s'en remet au vendeur.
NO_PREFERENCE_PATTERNS = [
    r"^\s*(peu|peut)\s+import(e|es)\b",
    r"^\s*(n'?importe|nimporte)\b",
    r"^\s*(comme tu veux|comme vous voulez|a toi de voir|c'?est toi)\b",
    r"^\s*(je (m'?en (fous|fiche)|sais pas|ne sais pas))\b",
    r"^\s*(aucune?( preference| idee)?|pas de preference)\s*$",
    r"^\s*(non|nan|nope)\s*$",
    r"^\s*(oui|ok|okay|d'?accord|vas[- ]?y|allez|go|montre|montre moi)\s*[!.]?\s*$",
    r"^\s*(tout|tous|les deux|peu m'?importe)\s*$",
]

# Mots trop génériques pour identifier un produit dans le catalogue.
_STOPWORDS = {
    "je", "tu", "il", "elle", "on", "nous", "vous", "ils", "me", "moi", "te",
    "toi", "se", "un", "une", "des", "du", "de", "la", "le", "les", "l", "d",
    "et", "ou", "mais", "donc", "car", "que", "qui", "quoi", "pour", "avec",
    "sans", "dans", "sur", "sous", "veux", "voudrais", "cherche", "besoin",
    "avez", "as", "est", "es", "ai", "a", "au", "aux", "ce", "cet", "cette",
    "ca", "cela", "bonjour", "salut", "merci", "stp", "svp", "montre", "voir",
    "photo", "image", "prix", "budget", "combien", "fcfa", "franc", "francs",
    # Mots de demande d'alternative / émotion : ne doivent PAS polluer la
    # requête catalogue (sinon "propose moi autre chose je n'aime pas la
    # couleur rose" cherchait "chose aime couleur" et renvoyait des téléphones).
    "propose", "proposes", "autre", "autres", "chose", "aime", "aimes",
    "couleur", "couleurs", "remplace", "option", "options", "modele", "modeles",
    "marque", "marques", "prefere", "preferes", "envoie", "montrer", "donne",
    "donnes", "mets", "met", "cherches", "voudrais", "aimerais",
    # Besoins généraux : la CATÉGORIE fait le travail, ces mots ne doivent pas
    # polluer la requête FTS5 ("cadeau femme" -> beaute, pas "cadeau femme").
    "cadeau", "cadeaux", "offrir", "offre", "femme", "femmes", "homme",
    "hommes", "anniversaire", "cuisiner", "manger", "repas", "cuisine",
    "aliment", "gouter", "dejeuner", "besoin", "besoins", "acheter", "achat",
    # "produit" est générique : "un produit pour ma femme" -> la CATÉGORIE
    # fait le travail. Chercher "produit" seul renvoyait du bruit FTS5
    # (Samsung au lieu de produits beauté).
    "produit", "produits", "article", "articles", "truc", "choses", "modele",
    # Négations et adjectifs de préférence : ne sont pas des critères produits.
    # "je n'aime pas la couleur rose" -> ne doit pas chercher "pas rose".
    "pas", "plus", "tres", "trop", "vraiment", "beaucoup", "assez", "bien",
    "mal", "joli", "jolie", "beau", "belle", "super", "magnifique", "sympa",
    # Couleurs seules : rarement un critère FTS5 utile (le catalogue a des noms
    # de produits, pas des variantes couleur).
    "rose", "noir", "noire", "bleu", "bleue", "blanc", "blanche", "rouge",
    "vert", "verte", "jaune", "gris", "grise", "violet", "violette", "or",
    "argent", "glacier",
}


def _norm(text: str) -> str:
    """Minuscule sans accents, ponctuation réduite à des espaces."""
    text = unicodedata.normalize("NFD", (text or "").lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def detect_category(text: str):
    """Retourne la famille de produits évoquée, ou None.

    Les mots-clés multi-mots ("barre de son") sont testés avant les mots simples
    afin que "barre de son" ne soit pas classé via un mot isolé.
    Le pluriel français est géré : "frigos" matche "frigo", "TVs" matche "tv".
    """
    t = f" {_norm(text)} "
    best, best_len = None, 0
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            k = _norm(kw)
            if not k:
                continue
            if " " in k:
                pattern = f" {k} "
                matched = pattern in t
            else:
                # singulier/pluriel : \bfrigos?\b  (s final optionnel)
                pattern = rf"\b{re.escape(k)}s?\b"
                matched = re.search(pattern, t)
            if matched:
                if len(k) > best_len:
                    best, best_len = cat, len(k)
    return best


def is_no_preference(text: str) -> bool:
    """True si le message n'apporte aucun critère de choix."""
    t = _norm(text)
    return any(re.search(p, t) for p in NO_PREFERENCE_PATTERNS)


# Demandes EXPLICITES de voir des produits : "montre moi les frigos",
# "je veux voir", "liste les", "qu'avez-vous en X". Dans ce cas le bot doit
# AFFICHER les produits, pas reposer une question de qualification.
_SHOW_PATTERNS = [
    r"montre[- ]?moi",
    r"montre\b",
    r"vois?\b.*(?:dispo|disponible|stock)",
    r"je veux voir",
    r"je voudrais voir",
    r"liste\b",
    r"qu\s*(?:est)?\s*ce\s*qu",  # qu'est-ce que vous avez
    r"qu\s*(?:avez|as)\s*(?:vous|tu)?\s*(?:de|en|comme)",  # qu'avez-vous comme X
    r"quels?\s*(?:sont\s*)?(?:les|vos)?\s*.*\b(?:dispo|disponibles?)\b",
    r"envoie",
    r"presente[- ]?moi",
    r"presente\b",
    r"affiche",
    # Demandes d'ALTERNATIVE : "propose moi autre chose", "tu as autre chose",
    # "autres options", "autre modèle". Le client veut VOIR d'autres produits.
    r"propose[- ]?moi",
    r"propose\b",
    r"autres?\s+(?:chose|option|modele|produit|marque)",
    r"d\s?autres?\b",
    r"tu\s+(?:as|n\s?as)\s+(?:pas\s+)?(?:de\s+)?(?:d\s?autres?|autre\b)",
    r"remplace\b",
    r"autres?\b.*\b(?:couleur|couleurs|modele|option)\b",
]


def wants_to_see_products(text: str) -> bool:
    """True si le client demande EXPLICITEMENT à voir les produits.

    Distingue "Montre moi les frigos" (=> afficher) de "j'ai besoin d'un frigo"
    (=> qualifier). C'est ce qui évite de re-questionner un client qui a déjà
    demandé la liste.
    """
    t = _norm(text)
    return any(re.search(p, t) for p in _SHOW_PATTERNS)


# Mots courts mais significatifs : ne pas les couper via len(w) > 2.
_SHORT_KEEP = {"tv", "pc", "4k", "8k", "hd", "ac"}


def _content_words(text: str) -> list:
    """Mots utiles pour une requête catalogue, stopwords et bruit retirés.

    Les nombres sont conservés : ils portent la taille ("55 pouces"),
    la capacité ("128 go") ou le modèle ("A76").
    """
    out = []
    for w in _norm(text).split():
        if w in _STOPWORDS:
            continue
        if w in _SHORT_KEEP or w.isdigit() or len(w) > 2:
            out.append(w)
    return out


def has_product_signal(text: str) -> bool:
    """True si le message contient un mot exploitable pour le catalogue.

    Un message sans préférence ("oui", "peu importe") ne compte pas comme un
    signal produit, même si ses mots survivent au filtrage des stopwords.
    """
    if is_no_preference(text):
        return False
    return bool(_content_words(text))


def clean_query(text: str) -> str:
    """Nettoie un message client pour en faire une requête catalogue."""
    return " ".join(_content_words(text)[:8])


def resolve_search_query(text: str, category: str = None) -> str:
    """Requête catalogue à utiliser, même sans critère explicite.

    Ordre : mots du message > catégorie mémorisée > chaîne vide (échantillon).
    C'est ce qui évite le `search("oui") -> 0 résultat` observé en production.
    """
    if has_product_signal(text):
        q = clean_query(text)
        if q:
            return q
    if category and category in CATEGORY_QUERY:
        return CATEGORY_QUERY[category]
    return ""


def track_category(text: str, current: str = None):
    """Suit la catégorie au fil de la conversation.

    Retourne (categorie_active, a_change). Un message sans catégorie détectable
    ("peu importe", "oui") conserve la catégorie courante : le client précise
    son besoin, il ne change pas de sujet.
    """
    found = detect_category(text)
    if found is None:
        return current, False
    if current is None:
        return found, False
    return (found, True) if found != current else (current, False)
