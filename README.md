# Colors — serveur Turf War reconstruit

Ce serveur Python/SQLite remplace le service disparu du jeu Gizmondo Colors. Il implémente les 25 commandes retrouvées dans le client, les crews, territoires, défenseurs, revenus virtuels, attaques et messages. Le paquet contient aussi les sources PocketHLE modifiées et un patch. Aucun fichier du jeu, manuel ou vidéo n’est redistribué.

Le manuel a été lu avec ses illustrations. La vidéo Turf Wars fournie a servi à vérifier les menus et le déroulement. La compatibilité du protocole a été confrontée aux instructions ARM originales ; une partie interactive complète à deux clients reste à valider. Le serveur historique et ses règles internes ne sont pas disponibles : les valeurs inconnues sont configurables, pas présentées comme des données historiques certaines.

## Démarrer le serveur

Python 3.10 ou ultérieur. Dans le dossier `colors-server` :

```sh
python -m pip install -r requirements.txt
python -m colors_server --host 0.0.0.0 --port 8080 --database data/colors.sqlite3 --tile-cache data/tiles --rules rules.example.json
```

Sur Linux, utiliser `python3` si nécessaire. `http://127.0.0.1:8080/health` répond pour vérifier le démarrage. Pour un téléphone sur le même réseau, saisir l’adresse LAN du PC serveur, par exemple `http://192.168.1.10:8080`. `127.0.0.1` sur un téléphone désigne ce téléphone.

Pour des joueurs distants, le même service doit être accessible par un nom de domaine ou une adresse publique, via un reverse proxy HTTPS ou un réseau privé commun. Le serveur n’est pas hébergé par ce paquet. L’adresse exacte à saisir dépend de cet hébergement. Conserver la route `/applications/games/colors/open/command.do`. Le proxy ne doit pas mettre en cache les POST. `COLORS_ACCESS_TOKEN` est facultatif et réclame un Bearer HTTP ; PocketHLE n’injecte pas ce token, donc laisser cette option vide pour une connexion directe du jeu.

Les données persistantes sont dans `data/`. Sauvegarder SQLite à l’arrêt, ou utiliser l’API de sauvegarde SQLite en ligne. Ne pas déplacer l’origine d’une base existante : ses identifiants de territoires en dépendent.

## PocketHLE

Le patch PocketHLE fourni séparément intègre :

- les champs **Colors multiplayer server (next launch)** et **Colors player ID (optional)**, côté desktop et Android ;
- la redirection de ce service historique uniquement ;
- une identité `HKLM\GTShell\TerminalID` créée une fois et conservée dans le registre de l’installation ;
- `_atoi64` avec résultat ARM 64 bits R0/R1, nécessaire aux timestamps.

Compiler les sources ; une ancienne version installée ne contient pas ces options. Sous Windows, avec la chaîne de compilation PocketHLE habituelle, depuis la racine du dépôt PocketHLE :

```powershell
cargo build --release -p pocket-desktop --features unicorn
.\target\release\pockethle-gui.exe
```

Suivre également le README PocketHLE pour les prérequis natifs. Pour Android, suivre `PocketHLE/frontends/pocket-android/README.md` et compiler l’APK avec le SDK/NDK prévu par le projet. Aucun EXE Windows ni APK compilé n’est fourni.

Dans les réglages du lanceur, saisir l’origine HTTP/HTTPS du serveur, sans chemin, activer GPS / host location et **GPRS/data** dans **Settings > Gizmondo options**, enregistrer et relancer le jeu. Android nécessite l’autorisation de localisation. Un PC dépend de la position fournie par Windows. Une absence de position réelle peut bloquer la carte. Le backend HTTP/GPS natif disponible dans cette source concerne Windows et Android ; le CLI Linux standard ne fournit pas ces backends. Un banc d’essai Linux avec transport HTTP local et fixtures GPS/carte a exécuté le jeu patché jusqu’à la carte Turf War ; cela ne valide pas le transport Windows ni une partie complète entre deux clients.

Chaque joueur doit avoir sa propre identité. Laisser le champ ID vide pour conserver l’identité de l’installation, ou choisir deux valeurs distinctes (`PlayerA`, `PlayerB`). Ne pas partager une copie du même registre entre deux joueurs. Pour deux instances desktop sur le même PC, lancer depuis deux consoles PowerShell avec deux bibliothèques distinctes :

```powershell
$env:POCKETHLE_LIBRARY = "C:\Colors\PlayerA"
.\target\release\pockethle-gui.exe
```

Dans l’autre console, utiliser `C:\Colors\PlayerB`. Importer le jeu dans chacune, régler le même serveur, et attribuer des IDs différents si des profils ont été copiés. Tout l’argent est virtuel. Le premier login demande un nom, puis synchronise la somme virtuelle envoyée par le jeu.

## Carte mondiale

La position GPS détermine toujours le fond local. Paris n’est qu’une origine mathématique immuable pour les IDs, pas une restriction de couverture. Les joueurs peuvent être dans des pays différents sur le même serveur. La longitude et les voisins sont périodiques : les territoires ne se dupliquent pas à la jonction mondiale. Les tuiles OSM passent aussi l’antiméridien. Le zoom s’adapte pour limiter à 16 les tuiles téléchargées par vue ; aucune précollecte mondiale.

La carte GIF fait 315×225 pixels, avec 7×5 cases et une attribution au-dessus du HUD. La grille géographique est régulière : environ 100 m nord-sud et 100 m est-ouest à la latitude de l’origine. Sa largeur physique est-ouest varie avec la latitude. La projection exacte du service disparu reste inconnue. L’alignement du fond et des limites utilise la même projection partout. Les tuiles raster OSM s’arrêtent vers ±85,05° : aux pôles, le serveur conserve la position et affiche explicitement l’absence de fond raster, sans inventer une carte.

Le cache conserve les tuiles, respecte les expirations et utilise ETag/Last-Modified pour revalidation. Modifier `--tile-agent` pour inclure un contact du service hébergé. Référence : https://operations.osmfoundation.org/policies/tiles/ . Pour un trafic important, choisir un fournisseur de tuiles adapté via `--tile-url`.

## Règles configurables

`rules.example.json` contient tous les paramètres. Les prix des défenseurs 2000/5000/7000, la collecte 1500 et le compte à rebours autour de deux minutes sont observés dans la vidéo de démonstration. Cela ne prouve pas leur valeur dans toute version du jeu. Le prix d’attaque 1001 est un repli trouvé dans le client. Prix des armes, plafonds, santé, précision, prix d’un territoire et délais non démontrés sont des choix de remplacement. Modifier ces paramètres sans toucher au protocole. Les délais JSON sont en secondes ; le serveur convertit les champs réseau requis en millisecondes.

Le combat se joue localement ; son résultat est déclaré par le client. La synchronisation de l’argent solo est activée pour respecter son parcours. L’identité matérielle n’est pas une authentification secrète. Ce paquet ne fournit donc pas de protection complète contre des clients modifiés ; il convient d’abord aux parties de confiance. Aucun paiement ou argent réel.

## Vérifier

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python tools/verify_arm_codec.py /chemin/Colors.exe
python tools/verify_arm_protocol.py /chemin/Colors.exe
```

Les scripts ARM exigent l’exécutable attaché dont le hash est décrit dans PROTOCOL.md. Ils n’en contiennent pas de copie. Le test du protocole utilise un vrai HTTP local et des tuiles fixtures ; option `--gif /chemin/carte.gif` pour décoder aussi une carte réelle avec le décodeur du jeu. Les adaptations de l’environnement ARM sont explicitement documentées dans les scripts : imports CRT, réseau déjà effectué et sortie GPU neutralisée. Ce n’est pas une exécution intégrale du jeu.

## Patch du blocage de connexion

Appliquer les fichiers PocketHLE et serveur ensemble. PocketHLE transmet les notifications GPRS attendues par le jeu et corrige la valeur GNS initiale. Le serveur fournit les dates absolues selon l’horloge GPS1 et l’image Base64 dans un nœud CDATA, indispensable au parseur original.

Sur Synology, remplacer les fichiers du projet existant puis reconstruire le projet dans Container Manager afin de reconstruire l’image Docker avec ces sources. Conserver le dossier `data/`. Un simple redémarrage de l’ancien conteneur ne charge pas les changements.

Les logs du conteneur affichent les commandes reçues, leur résultat et leur durée, ainsi que le statut HTTP. Ils ne journalisent pas les corps de requête, identités, positions ou messages. `NoSuchUserException` peut être normal avant la création du premier joueur.

## Reconnexion

La reconnexion du jeu patché a été vérifiée après redémarrage du jeu et du serveur : le joueur retrouve sa carte sans recréation du profil. Conserver côté PocketHLE le dossier Flash Disk et les sauvegardes. Colors stocke TWPlayerValid dans config.cfg à la racine de son dossier Flash Disk, en dehors de Save. Le lanceur desktop conserve déjà ce dossier. Voir `docs/VALIDATION.md` pour la cause du problème du banc d’essai et les limites du test.

## Server-side public hosting hardening

See deploy/README.md for Docker, an HTTPS reverse proxy, request limiting,
persistent storage and consistent online backups. The wire protocol is unchanged.
XML depth/element/attribute limits, bounded tile cache/downloads, graceful shutdown
and SQLite busy-error handling are enforced server-side. The optional access token
remains optional; no new client authentication is required. Public deployment
still requires the proxy/firewall and monitoring documented there.


### Correctif carte Paris / « Erreur de transfert »

Les IDs de territoires envoyés à Colors sont maintenant strictement positifs :
les coordonnées internes signées sont décalées de +1 000 000 sur chaque axe sur
le réseau, puis reconverties pour toute commande reçue. Le client retail refuse
les coordonnées nulles ou négatives après avoir affiché le GIF. Les degrés GPS,
la grille, les cartes et les clés SQLite ne changent pas. Conserver la base et
ses volumes, redéployer/redémarrer le serveur, puis rouvrir Turf Wars pour
rafraîchir les IDs. Voir PROTOCOL.md pour le contrat et la validation.
