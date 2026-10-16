# Prompt d'installation à donner à Claude depuis le PC du bord

Copier tout le bloc ci-dessous dans Claude Code (ou l'app Claude) sur le PC.

---

Je suis à bord de mon voilier et j'installe le tracker de position du bateau sur
un Raspberry Pi 4B. Tout le code et la doc sont dans le repo public
https://github.com/enguelm/pfp-tracker : lis d'abord son README.md en entier,
puis guide-moi étape par étape, en français, une étape à la fois, en attendant
ma confirmation avant de passer à la suivante. Ne modifie pas le code du repo
sauf bug avéré, et dans ce cas explique-moi le correctif avant.

Matériel et réseau, déjà en place :
- Bus NMEA 2000 Raymarine : iTC-5 (vent, loch, sondeur), AIS700 qui fournit la
  position GPS, la vitesse et le cap fond. Compas : EV-1 du pilote Raymarine
  Evolution (cap magnétique, publié seulement quand le pilote est sous tension).
- Passerelle Yacht Devices YDEN-03N branchée en Ethernet sur le routeur
  Teltonika RUTM10. Réglages usine : flux NMEA 0183 sur TCP 1456, interface web
  http://yden.local, mot de passe admin.
- Routeur RUTM10 : Starlink sur le port WAN, ce PC sur LAN1. LAN usine
  192.168.1.1/24, à passer en 192.168.10.1/24 pour éviter la collision avec le
  routeur Starlink.
- Raspberry Pi 4B, carte SanDisk High Endurance 64 Go, à brancher en Ethernet
  sur le RUTM10. Alimentation USB-C 3 A.
- Ce PC est sous [Windows / macOS / Linux : à préciser].

Ce que j'attends de toi, dans l'ordre :
1. Vérifier le RUTM10 avec moi : Internet traverse-t-il le routeur, le port
   Starlink est-il en rôle WAN, passer le LAN en 192.168.10.1/24, poser des
   baux DHCP statiques pour la passerelle et le Pi.
2. Vérifier la passerelle : LED Ethernet qui clignote, interface web
   accessible, phrases $YDRMC et $YDMWD visibles sur le port 1456 depuis ce PC
   (donne-moi l'outil adapté à mon système).
3. Flasher la carte SD avec Raspberry Pi Imager : Raspberry Pi OS Lite 64 bits,
   hôte pfp-pi, utilisateur pi, SSH activé, fuseau UTC, pas de WiFi.
4. Premier démarrage : me connecter en SSH au Pi, récupérer le code avec la
   commande curl du README (le Pi a Internet par Starlink), lancer install.sh,
   puis check.sh --sample et m'aider à interpréter la sortie. Si yden.local ne
   se résout pas, mettre l'IP fixe de la passerelle dans
   ~/pfp-tracker/config.env.
5. Me rappeler de faire une image de la carte SD une fois que tout tourne.

Supabase n'est pas encore déployé : c'est normal que l'envoi soit « non
configuré », les points doivent s'accumuler dans la file locale. Ne cherche pas
à déployer Supabase ni à toucher au TRACKER_TOKEN.

Avant toute commande qui efface ou écrase quelque chose (formatage de la carte,
reset du routeur, suppression de fichiers), demande-moi confirmation.
