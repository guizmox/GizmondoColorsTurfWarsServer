# Protocole Colors Turf War

## Sources et version

Analyse de l’exécutable ARM attaché `Colors.exe`, SHA-256 :

`addaca723aba2dd4beb7449bbbd37a89988cb0345ee5381e08f2c18dc62473cf`

Base PE `0x10000`, entrée `0x126a08`. Manuel complet, texte et illustrations ; spreads 15–18 pour Turf War. Vidéo fournie, environ 310 secondes : collecte, gestion du crew, défenseurs, carte, compte à rebours et combat local. La vidéo est une démo ; les règles non déductibles restent paramétrables.

`colors.zip` est un squelette de placeholders. Sa forme XML simplifiée ne correspond pas aux parseurs retail : elle n’a pas été conservée comme contrat réseau.

## Transport retrouvé

Hôte original : `us.mygiz.gizmondo.com`.

Route : `POST /applications/games/colors/open/command.do`.

Content-Type : `application/x-www-form-urlencoded`.

```text
command=COMMAND&data=<c>BASE64(TRANSFORM(XML_UTF8 + NUL))</c>
```

Les valeurs du formulaire sont encodées URL après construction : `+` doit rester une donnée Base64, pas un espace. Les réponses sont du XML en clair. Le serveur accepte aussi `/colors.php` pour les outils utilisant l’ancien nom de squelette.

La transformation réversible utilise la clé et les tables retrouvées dans les données du binaire, avec retour de l’octet précédent. Ce n’est pas une authentification. `codec.py` est testé byte pour byte avec les instructions ARM pour 12 tailles traversant les frontières de blocs. Il refuse DOCTYPE, ENTITY et les enveloppes excessives.

## Commandes

La racine XML de la requête diffère parfois du nom de commande. Identité généralement `terminalhardwareid`, sauf `sendertermhwid` pour les messages et `getganginfo` adressé par `gangid`.

| Commande | Racine requête | Fonction |
|---|---|---|
| `getgamevars` | `getgamevars` | Règles et timestamps |
| `synctimestamps` | `synctimestamps` | Accusé de synchronisation |
| `getuserinfo` | `getuserinfo` | Profil, argent virtuel, appartenance |
| `setuserinfo` | `user` | Nom et argent solo déclaré |
| `getmapview` | `getmapview` | GPS → GIF, 35 turfs, crews |
| `buyturf` | `buyturf` | Acheter un turf libre ou mis en vente |
| `creategang` | `creategang` | Créer le crew ET acheter le premier turf |
| `joingang` | `joingang` | Rejoindre par mot de passe ET acheter le premier turf |
| `leavegang` | `leavegang` | Quitter le crew |
| `dissolvegang` | `dissolvegang` | Dissoudre et libérer ses territoires |
| `removeplayer` | `removeplayer` | Supprimer le profil |
| `getganginfo` | `getganginfo` | Crew et membres |
| `setganginfo` | `setganginfo` | Modifier nom/couleur/tag/mot de passe |
| `abdicate` | `abdicate` | Transférer le leadership |
| `releasegangmember` | `releasegangmember` | Exclure un membre |
| `getturflist` | `turfrequest` | Liste des territoires du crew |
| `setturfprice` | `turfprice` | Prix de vente |
| `setturfinfo` | `setturfinfo` | Nom du turf |
| `tagturf` | `tagturf` | Texte rival sur un turf |
| `buyhomeboy` | `buyhomeboy` | Acheter des défenseurs de niveaux 1–3 |
| `organizehomeboys` | `organizehomeboys` | Déplacer les défenseurs en conservant chaque total |
| `collectincome` | `collectincome` | Collecter les périodes écoulées, une fois |
| `attackturf` | `turfattack` | initiate → commence → report, ou cancel |
| `sendsmsmessage` | `sendsmsmessage` | Message virtuel interne au jeu |
| `getmessages` | `getmessages` | Lire les messages non livrés |

Les tests donnent les corps complets des 25 requêtes. Tous ont aussi été envoyés au serveur HTTP en scénario multi-joueur, avec réouverture de la base pour contrôler la persistance.

## Détails indispensables au client

- Premier `getuserinfo` : erreur `NoSuchUserException` avant création, pour ouvrir la saisie du nom. `setuserinfo` enregistre ensuite ce nom et l’argent virtuel du solo.
- Format d’erreur : `<error><reason><![CDATA[exceptions.NoSuchUserException: message]]></reason></error>`. Le parseur ARM lit la valeur directe du nœud reason ; un simple nœud texte ne permettait pas de reconnaître l’exception. Les erreurs métier sont en HTTP 200.
- `getuserinfo` renvoie directement `<user>`, `getganginfo` directement `<gang>` ; les mutations transportent ces objets dans leur réponse.
- `getgamevars` contient `timestamp.current`, `timestamp.incremented`, puis des `<var><name>...</name><value>...</value></var>`. Ce ne sont pas des sous-arbres PHP imbriqués par points.
- Les dates absolues (`server.time`, `lastcollect`, `timestamp`) sont en millisecondes depuis le 01/01/1972, pour correspondre au paquet GPS1 que Colors compare directement à son horloge serveur. SQLite conserve des secondes Unix : seule la sérialisation réseau change. La valeur zéro reste une date absente. Les durées `turf.income.waitingtime` / `turf.timestamp.commence.delay` restent en millisecondes. Le client divise le compte à rebours par 1000. Un délai de 120 secondes doit donc être envoyé comme 120000.
- Couleur de crew : trois canaux 16.16, chacun 0..65536, séparés par virgules. Tag de crew non vide requis par son parseur ; si absent, le serveur emploie le nom du crew.
- Chaque turf contient `name`, `tag`, `gangid`, `price`, `state`, `lastcollect`, `timestamp`, `coordinates`, `homeboys`. États : `normal`, `underattack`, `fightinprogress`.
- Défenseurs en requête : `homeboy/type` et `homeboy/number`. En réponse : `homeboy/type` et `homeboy/value`.
- Le menu du premier turf envoie directement `creategang` ou `joingang` : achat et entrée dans le crew sont atomiques, avec annulation complète si le mot de passe ou les fonds sont invalides.
- Le client rapporte le combat : `attackturf action="report"`, `outcome winner="attacker"` ou `"owner"`, avec `homeboyslost`. Ce serveur ne simule pas un combat distant.

## Cartes et mondialisation

`getmapview/xpos` est la longitude ; `ypos` est la latitude. Image GIF 315×225, cases 45 pixels. 35 territoires listés d’ouest en est, rangées du SUD vers le NORD ; le fond lui-même est nord en haut. Le centre est à l’indice 17. `xoffset` / `yoffset` sont des fractions de case en pixels. Tous les IDs de crews présents dans les turfs doivent avoir leurs métadonnées dans `gangs` ; le client a une capacité de 36 crews par vue.

La grille du remplacement a une origine fixe, des longitudes périodiques et des IDs canoniques ; les distances de voisinage tiennent compte du bouclage. La grille et le fond OSM sont calculés avec les mêmes coordonnées géographiques. Zoom adaptatif et cache à la demande, aucun chargement de carte mondiale entière. Les pôles restent adressables ; hors couverture raster OSM, le fond indique son absence. Dimensions physiques des cases variables avec la latitude, comme détaillé dans README.md : ce choix est explicite, la projection historique n’a pas été récupérée.

## Adresses utiles, dans cette version seulement

| Adresse ARM | Fonction observée |
|---|---|
| `0xe4bc8` | Lecture du TerminalID du registre |
| `0xe9a08`, `0xe9ae4` | Transformation / inverse |
| `0xf4d24`, `0xf4b60` | Base64 / encodage URL |
| `0xf4848` | Analyse de réponse et exceptions |
| `0xe579c` | Callback carte |
| `0xf274c`, `0xf1a2c`, `0xf1ea0` | Parseurs turf, crew, user |
| `0xe53d0`, `0xe5528` | Lecture de règles int / temps |
| `0xdb174..0xdb190` | Conversion du délai ms en secondes |
| `0xa8010` | Décodeur GIF du jeu |
| IAT `0x1289b0`, ordinal 1418 | `_atoi64` |

Les scripts exécutent ces fonctions originales sous Unicorn. Ils remplacent les imports CRT nécessaires, injectent la réponse HTTP déjà reçue dans le callback carte et neutralisent la création GPU. Les analyses XML, crews, turfs, variables, GIF et l’encodage des requêtes restent des instructions originales. Ces preuves ne démontrent pas une partie complète dans le lanceur.

Le contenu Base64 de `<image-data>` doit être placé dans CDATA : le callback du jeu accède directement au stockage de ce nœud enfant. Un simple texte XML provoque un accès nul, même si un parseur XML standard accepte la réponse.
