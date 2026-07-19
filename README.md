# Bot Discord — Paliers vocaux

Ce bot comptabilise le temps vocal actif de chaque membre et publie une annonce lorsqu'un palier est atteint.

## Ce qui est compté

Le temps est compté uniquement lorsque le membre :

- est connecté à un salon vocal ou à une scène ;
- n'est pas dans le salon AFK du serveur ;
- n'est pas en sourdine personnelle ou serveur ;
- n'est pas en mode sourd personnel ou serveur ;
- n'est pas un bot.

Le temps déjà accumulé reste enregistré dans SQLite après un redémarrage. Le temps pendant lequel le bot est éteint n'est pas compté.

## Paliers

1. 1 heure
2. 6 heures
3. 12 heures
4. 1 jour
5. 2 jours
6. 3 jours
7. 1 semaine
8. 2 semaines
9. 1 mois
10. 2 mois
11. 3 mois
12. 4 mois
13. 5 mois
14. 6 mois
15. 7 mois
16. 8 mois
17. 9 mois
18. 10 mois
19. 11 mois
20. 1 an

Un mois correspond à 30 jours et une année à 365 jours.

## Installation

### 1. Créer l'application Discord

1. Ouvre le portail développeur Discord.
2. Crée une application, puis un bot.
3. Copie le token du bot.
4. Dans **Bot > Privileged Gateway Intents**, active **Server Members Intent**.
5. Dans le générateur d'URL OAuth2, sélectionne les scopes :
   - `bot`
   - `applications.commands`
6. Permissions recommandées :
   - Voir les salons
   - Envoyer des messages
   - Intégrer des liens
   - Gérer les rôles
7. Invite le bot sur ton serveur.
8. Dans les paramètres des rôles du serveur, place le rôle du bot au-dessus des rôles qu'il devra attribuer.

### 2. Installer Python et les dépendances

Python 3.11 ou plus récent est recommandé.

```bash
python -m venv .venv
```

Sous Windows :

```bash
.venv\Scripts\activate
```

Sous Linux/macOS :

```bash
source .venv/bin/activate
```

Puis :

```bash
pip install -r requirements.txt
```

### 3. Configurer les variables

Copie `.env.example` vers `.env`, puis ajoute le token :

```env
DISCORD_TOKEN=ton_token_ici
DATABASE_PATH=voice_milestones.sqlite3
```

Pendant les tests, tu peux renseigner l'identifiant de ton serveur afin que les commandes slash apparaissent immédiatement :

```env
TEST_GUILD_ID=123456789012345678
```

Une fois le bot prêt pour plusieurs serveurs, supprime `TEST_GUILD_ID`. Les commandes globales peuvent prendre du temps à apparaître.

### 4. Lancer le bot

```bash
python bot.py
```

## Commandes

### Pour tous les membres

- `/vocal` : affiche ton temps vocal.
- `/vocal membre:@Quelquun` : affiche le temps vocal de ce membre.
- `/paliers-vocaux` : affiche tous les paliers.

### Pour les administrateurs

- `/config-vocal salon` : choisit le salon des annonces.
- `/config-vocal role` : associe un rôle à un palier.
- `/config-vocal retirer-role` : retire l'association d'un rôle.
- `/config-vocal voir` : affiche la configuration.
- `/config-vocal synchroniser-roles` : donne les rôles déjà mérités aux membres existants.

## Exemple de configuration

```text
/config-vocal salon salon:#paliers-vocaux
/config-vocal role palier:1 role:@Membre
/config-vocal role palier:7 role:@Habitué
/config-vocal role palier:20 role:@Vétéran
```

Les rôles sont cumulatifs : atteindre un nouveau palier ne retire pas automatiquement les rôles précédents.

## Hébergement

Le bot doit fonctionner en continu pour mesurer correctement le vocal. Il peut être hébergé sur un VPS, un Raspberry Pi, un ordinateur allumé en permanence ou une plateforme prenant en charge un processus Python persistant et un fichier SQLite persistant.
# VoiceTracker
