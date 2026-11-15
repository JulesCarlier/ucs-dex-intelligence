# DEX INTELLIGENCE — U.C.S.

Cette livraison est une première version intégrée du projet : interface web, API backend, stockage SQLite et scanner Python avec synchronisation optionnelle.

## Contenu

- `index.html` : interface web (tableau de bord, scanner, candidats, assistant explicatif, simulation, connexion API).
- `backend/main.py` : API FastAPI, réception sécurisée des détections, snapshots, indicateurs de suivi et simulation virtuelle.
- `scanner/scanner.py` : scanner adapté à partir du code fourni. Il conserve les alertes Telegram et transmet aussi les détections et checkpoints à l'API si les variables d'environnement sont renseignées.
- `scanner/scanner_original_reference.py` : copie de référence du fichier source transmis, non modifiée.

## Important — état réel des fonctions

- La détection et les alertes Telegram restent pilotées par le scanner d'origine.
- Les données sont transmises au backend seulement après configuration de `DEXI_API_URL` et `DEXI_INGEST_API_KEY`.
- La base stocke les détections et les snapshots de checkpoint (5 min, 15 min, 1 h, 24 h).
- La confirmation est indépendante du score : deux snapshots consécutifs doivent avoir buy ratio >55 %, change 5 min entre 0 et +8 %, et liquidité >20 000 $.
- L'évolution compare les scores des deux derniers snapshots.
- La simulation est entièrement virtuelle. Aucun portefeuille, ordre ou transfert n'est connecté.
- L'assistant fourni est un assistant explicatif de base, pas un modèle IA génératif. Une IA générative nécessitera un fournisseur, une clé secrète côté serveur, des limites de coût et une configuration sécurisée.
- La base SQLite est adaptée au test local. Pour un déploiement public durable, il faut un stockage persistant ou une base PostgreSQL ; ne pas supposer que le disque d'un hébergement éphémère conserve les données.

## Tester le backend en local

Dans un terminal, depuis le dossier du projet :

```bash
python -m venv .venv
```

Windows :

```bat
.venv\Scripts\activate
pip install -r requirements.txt
set DEXI_INGEST_API_KEY=REMPLACE_PAR_UNE_LONGUE_CLE_ALEATOIRE
uvicorn backend.main:app --reload
```

L'API répond sur `http://127.0.0.1:8000/health` et la documentation sur `http://127.0.0.1:8000/docs`.

## Connecter le scanner (sur le PC)

Dans un second terminal Windows, définir les mêmes valeurs :

```bat
set DEXI_API_URL=http://127.0.0.1:8000
set DEXI_INGEST_API_KEY=REMPLACE_PAR_LA_MEME_LONGUE_CLE
python scanner\scanner.py
```

Le scanner continuera à envoyer ses alertes Telegram. S'il n'y a pas d'URL ou de clé API configurée, il fonctionne comme avant, sans synchronisation web.

## Déploiement

Le frontend statique peut rester sur Vercel. Le backend Python doit être déployé séparément (service web Python ou Docker) avec :

- `DEXI_INGEST_API_KEY` défini dans les variables secrètes de l'hébergeur ;
- `DEXI_DB_PATH` pointant vers un stockage persistant, ou remplacement SQLite par PostgreSQL ;
- `DEXI_CORS_ORIGINS=https://ucs-dex-intelligence.vercel.app` ;
- HTTPS activé.

Après déploiement, saisir l'URL publique du backend dans l'onglet **Connexion** de la Mini App. Ne jamais mettre la clé d'ingestion dans le frontend ou dans GitHub.

## Sécurité

Ne téléverse jamais `scanner_config.json` : il contient le token de ton bot Telegram. Ne publie aucune clé API ni mot de passe dans GitHub. Ne mets aucun secret dans `index.html`.
