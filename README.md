# BotWhatsApp — Vente Conversationnelle Intelligente

Bot de vente WhatsApp hybride (IA + supervision humaine) connecté à l'API WhatsApp Cloud de Meta.

## Architecture

```
WhatsApp → Webhook → FastAPI → SalesAgent (LLM) → WhatsApp
                         ↕
                   Dashboard (supervision humaine)
```

## Fichiers

| Fichier | Rôle |
|---------|------|
| `server.py` | Serveur FastAPI — webhook WhatsApp + API dashboard |
| `whatsapp_client.py` | Client API WhatsApp Cloud (envoi messages, médias) |
| `conversation_manager.py` | Gestion conversations SQLite (états, historique) |
| `sales_agent.py` | Agent de vente LLM — pipeline qualification→closing |
| `dashboard.html` | Interface web de supervision en direct |
| `config.yaml` | Configuration (tokens, mode, pipeline de vente) |

## Prérequis

1. **Compte Meta Business** → [business.facebook.com](https://business.facebook.com)
2. **App Facebook Developer** avec WhatsApp activé
3. **Numéro de téléphone** dédié WhatsApp Business
4. **Python 3.11+**

## Installation rapide

```bash
cd C:\Users\LEO\projects\BotWhatsApp
pip install -r requirements.txt
```

## Configuration Meta WhatsApp Cloud

### 1. Créer l'app Meta
- Va sur [developers.facebook.com](https://developers.facebook.com)
- Crée une app → type "Business"
- Ajoute le produit **WhatsApp**

### 2. Configurer WhatsApp
- Dans le dashboard WhatsApp, ajoute un numéro de test (gratuit, 5 destinataires max)
- Récupère :
  - **Phone Number ID** (dans "Configuration de l'API")
  - **WhatsApp Business Account ID**
  - **Access Token** (token temporaire ou permanent)

### 3. Remplir config.yaml
```yaml
whatsapp:
  phone_number_id: "123456789012345"
  business_account_id: "987654321098765"
  access_token: "EAAx..."
  verify_token: "bot_verify_token_2024"  # à personnaliser
```

### 4. Configurer le webhook
- URL de callback : `https://TON_DOMAINE/webhook/whatsapp`
- Token de vérification : même que `verify_token` dans config.yaml
- Champs à souscrire : `messages`

> Pour du développement local, utilise [ngrok](https://ngrok.com) :
> ```bash
> ngrok http 8000
> ```
> Puis mets l'URL ngrok dans la config webhook Meta.

### 5. Personnaliser le contexte de vente
Dans `config.yaml`, section `sales` :
```yaml
sales:
  product_description: "Décris ton produit/service ici"
  pricing: "Tes tarifs"
  unique_selling_points:
    - "Argument 1"
    - "Argument 2"
```

## Lancement

```bash
python server.py
```

Le dashboard est accessible sur `http://localhost:8000`

## Modes de fonctionnement

| Mode | Comportement |
|------|-------------|
| `auto` | Le bot répond automatiquement, jamais de validation humaine |
| `hybrid` | Réponses auto si confidence ≥ 85%, sinon validation humaine requise |
| `assist` | Toute réponse nécessite une validation humaine avant envoi |

## Pipeline de vente

1. **Qualification** — Identifier si prospect sérieux
2. **Découverte** — Besoins, budget, urgence
3. **Recommandation** — Solution adaptée
4. **Objection** — Répondre aux doutes
5. **Closing** — Passer à l'action

## Dashboard

![Dashboard](dashboard-preview.png)

- Liste des conversations en temps réel
- Messages en attente de validation (mode hybride/assist)
- Approuver / Modifier / Rejeter les suggestions IA
- Envoyer des messages manuels
- Stats en direct

## Limitations API WhatsApp Cloud

- **Numéro de test** : 5 destinataires max, messages illimités gratuits
- **Numéro business vérifié** : 1000 conversations/mois gratuites, puis payant
- **Templates** requis pour initier une conversation (délai 24h après dernier message client)
- **Médias** : images/documents hébergés publiquement (URL accessible)
