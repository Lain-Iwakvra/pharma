# Recherche psychopharmacologique

Projet Python qui, pour une molécule donnée :

1. Interroge l'**API GraphQL de PsychonautWiki (Bifrost)** —
   `https://api.psychonautwiki.org/` — pour récupérer des données
   **structurées** (classification, noms communs, potentiel addictif,
   toxicité, tolérance, interactions, effets, et pour chaque voie
   d'administration / ROA : dosage, durée d'apparition/come-up/pic/offset,
   biodisponibilité).
2. Récupère la **page MediaWiki de PsychonautWiki** (wikitext) pour
   compléter les champs absents de GraphQL, notamment la **demi-vie**
   (*half-life*).
3. Récupère la **page Wikipedia en langue anglaise** (API MediaWiki de
   `en.wikipedia.org`, avec la librairie de Goldsmith `wikipedia` si
   disponible, sinon appel direct à l'API) pour compléter (statut légal,
   toxicité détaillée, chimie…), y compris les lignes du tableau
   « Pharmacokinetic data » de l'infobox : liaison protéique
   (*protein binding*), métabolisme, métabolites, demi-vie,
   biodisponibilité.
4. Fusionne et affiche les informations pharmacologiques les plus
   importantes : dosage, voie(s) d'administration, temps d'apparition
   (*onset*), pic plasmatique (*Tmax / peak*), demi-vie,
   biodisponibilité, durée d'action, effets résiduels, toxicité, potentiel
   addictif, statut légal.

Les données PsychonautWiki sont prioritaires (wiki spécialisé dans les
produits de recherche), complétées par Wikipedia quand nécessaire.

Cette approche s'inspire de
[pw-cli](https://github.com/pharmacologic/pw-cli), qui interroge le même
API GraphQL Bifrost.

Deux façons de s'en servir, sur le même coeur de parsing :

| Interface | Fichier | Usage |
|-----------|---------|-------|
| Ligne de commande | `recherche_molecule.py` | recherche ponctuelle, export JSON/Markdown |
| Application de bureau | `pharma_studio.py` | atelier 3 volets : fichiers, documents, résumé |

## Installation

```bash
cd /svr/link/projet/pharma
pip install -r requirements.txt
```

> Si votre environnement Python bloque l'installation (PEP 668), utilisez
> `pip install --break-system-packages -r requirements.txt` ou créez un
> environnement virtuel (`python3 -m venv .venv && source .venv/bin/activate`).

## Utilisation — ligne de commande

```bash
# Affichage à l'écran seulement
python3 recherche_molecule.py LSD

# Avec sauvegarde du rapport (JSON et/ou Markdown)
python3 recherche_molecule.py "3-MMC" --save json --save md
python3 recherche_molecule.py MDMA --save md
```

Options :

* `molecule` : nom de la molécule à rechercher (positionnel, requis).
* `--save json` / `--save md` : enregistre le rapport dans un fichier
  `recherche_<molecule>.json` et/ou `.md` (répétable).

Quand plusieurs substances correspondent au nom saisi et qu'aucun titre exact
n'existe, le choix ne se fait jamais en silence :

* en **ligne de commande interactive**, un menu numéroté est proposé dans le
  terminal ;
* en **ligne de commande non interactive** (flux redirigé, cron…), le script
  liste les candidats et s'arrête avec le code de sortie `1` ;
* dans **l'application de bureau**, une fenêtre liste les candidats et la
  tâche reprend avec le titre retenu (jamais de question dans un terminal).

Wikipedia est toujours interrogé en anglais, comme demandé.

## Exemple de sortie

```
> Recherche de 'LSD' sur PsychonautWiki (GraphQL Bifrost) ...
> Recherche de 'LSD' sur Wikipedia (anglais) ...

############################################################
# Rapport psychopharmacologique : LSD
############################################################

──── PHARMACOLOGIE — CHAMPS CIBLES FUSIONNES ────

  Nom
    LSD

  Noms communs
    LSD; LSD-25; Lucy; L; Acid; Cid; Tabs; Blotter

  Voie(s) d'administration
    Sublinguale

  Dosage
    Sublinguale: seuil: 15.0 ; leger: 15.0-75.0 ; commun: 75.0-150.0 ;
    fort: 150.0-300.0 ; lourd: 300.0 ; (µg)

  Temps d'apparition (onset)
    15.0-30.0 minutes

  Pic plasmatique (Tmax / peak)
    3.0-5.0 hours

  Biodisponibilite
    Sublinguale: 71.0%

  Demi-vie (half-life)
    175 minutes

  Potentiel addictif / dependance
    non-addictive with a low abuse potential

  Toxicite
    extremely low toxicity relative to dose
```

Aucune étiquette de provenance n'est affichée : chaque information figure une
seule fois, que la valeur vienne d'un seul site ou des deux.

## Application de bureau (`pharma_studio.py`)

Atelier à trois volets, façon éditeur de code, pour **organiser** un dossier de
travail, **lire** les documents et **consulter** le résumé d'une molécule en même
temps :

```
┌────────────────┬──────────────────────────┬──────────────────────┐
│  Explorateur   │  Lecteur de documents    │   Résumé (parser)    │
│ (racine +      │  PDF · texte · HTML ·    │  tableau unifié      │
│  fichiers)     │  images · EPUB · onglets │  (resume.html)       │
└────────────────┴──────────────────────────┴──────────────────────┘
```

```bash
python3 pharma_studio.py                    # demande le répertoire de travail
python3 pharma_studio.py --workspace ~/pharma
```

Au premier lancement, une boîte de dialogue demande le **répertoire de
travail** ; il est mémorisé pour les fois suivantes (`QSettings`).

La fenêtre comporte cinq pages, conservées en haut (`Ctrl+1` à `Ctrl+5`) :
**Recherche** (les trois volets ci-dessus), **Comparaison**, **Interactions**,
**Calendrier** et **Pharmacocinétique** — voir les sections correspondantes
plus bas.

### Organisation du répertoire de travail

Le **répertoire de travail lui-même** est le noeud racine de l'arborescence
(les dossiers voisins sont masqués) : toutes les actions du bouton « + »
s'y appliquent aussi.

```text
<racine>/
  LSD/                          <- dossier de molécule
    wikipedia_LSD.pdf           <- PDF de la page Wikipédia anglaise
    psychonautwiki_LSD.pdf      <- PDF de la page PsychonautWiki (rendu hors ligne)
    resume.html                 <- version affichée dans le volet droit
    resume.json                 <- données brutes (mêmes que la CLI)
    études/
      Hofmann 1943.pdf          <- hérite de la molécule « LSD »
  .corbeille/                   <- corbeille interne (suppressions réversibles)
```

Le dossier d'une molécule ne contient que l'essentiel (deux PDF et deux
résumés) : `resume.md` n'est plus généré — un fichier hérité est retiré au
moment de la régénération, pour ne pas garder deux fois le même contenu —
et `.pharma.json` n'est créé que par une association **explicite**
(*Associer à une molécule…*).

Un document appartient à la molécule de son **dossier ancêtre le plus proche**
(sous la racine) : c'est lui qui fournit le résumé affiché à droite. Les
ancêtres sont examinés **du plus proche au plus loin** et le premier « signe
de molécule » gagne — `.pharma.json` d'abord, puis fichier `resume.*`. Seuls
comptent les dossiers qui **ressemblent** à une molécule : un dossier générique
(« notes », « etudes », « 2024 »…) n'est jamais deviné être une molécule, le
volet reste alors sur le message d'association.

**La racine du répertoire de travail n'est jamais une molécule**, même si elle
porte un `.pharma.json` ou un `resume.*` : sans cette règle, une association
posée sur la racine (par erreur) faisait croire que **tout** le travail
appartient à une seule molécule — le résumé restait bloqué sur elle partout,
et les inventaires (vues groupées, comparaison) en créaient un doublon. Le
menu contextuel de la racine n'offre donc ni *Associer à une molécule…* ni
*Régénérer le résumé* ; en sélectionner un élément ne change pas le volet
résumé.

Le lien entre un dossier et une molécule est rendu explicite : clic droit sur
un dossier → *Associer à une molécule…* ; **laisser le champ vide retire
l'association existante**.

Le titre du volet résumé affiche le **nom déclaré dans `resume.json`** quand il
diffère du nom de dossier, avec la mention `— dossier « … »` ; l'infobulle
donne le chemin complet du dossier source. Le titre et le contenu ne peuvent
donc plus se contredire (dossier renommé ou mal associé).

Le résumé se met à jour **automatiquement**, sans clic :

* tant qu'**un document est ouvert**, c'est **sa** molécule qui commande :
  une sélection accidentelle de l'explorateur (son modèle se remplit de façon
  asynchrone) ne remplace jamais le résumé du document lu ;
* sans document ouvert, une **simple sélection** dans l'explorateur (fichier
  d'une molécule ou dossier de molécule) affiche déjà son résumé ; un élément
  hors molécule ne touche pas au volet droit ;
* ouvrir un document — dans un onglet comme avec l'application par défaut —
  resynchronise le volet, y compris si l'onglet était déjà ouvert ;
* si `resume.html` manque alors que `resume.json` existe, le HTML est
  **reconstruit localement**, sans réseau ;
* si la molécule n'a encore aucun résumé **et que le dossier lui est
  explicitement associé** (`.pharma.json`), il est **récupéré en tâche de
  fond** (une seule tentative par dossier et par session) : le volet droit
  affiche « Récupération du résumé en cours… » puis le résultat. Le résultat
  ne s'affiche que si le dossier affiché est encore le bon (chemins résolus :
  un lien symbolique vers le même dossier reste le même dossier). Hors ligne,
  le bouton *Régénérer le résumé* reste disponible.

### Bouton « + » de l'explorateur

Disponible sur **la racine** et sur chaque dossier, via la barre d'outils ou
le clic droit :

| Action | Effet |
|--------|-------|
| **Créer un sous-dossier…** | nouveau dossier libre |
| **Ajouter une molécule…** | crée `<Molécule>/` avec le PDF Wikipédia, le PDF PsychonautWiki (rendu à partir de la page déjà récupérée, avec figures intégrées) et `resume.html` / `resume.json` ; plusieurs noms peuvent être saisis d'un coup, séparés par `;` (créés l'un après l'autre) |
| **Ajouter le PDF d'une page Wikipédia…** | télécharge le PDF de la page dont vous donnez le titre |
| **Ajouter le PDF d'une page PsychonautWiki…** | récupère la page PsychonautWiki et la rend en PDF dans le dossier choisi (figures intégrées, feuille de style d'impression) |
| **Ajouter un document existant…** | importe un document du disque (PDF, texte, image, HTML, EPUB) |
| **Rechercher un article ou un livre…** | cherche dans les sources **libres** et importe le PDF obtenu |

Le clic droit sur un dossier propose aussi *Régénérer le résumé* (relance le
parser sur les sources en ligne) et *Placer dans la corbeille* (déplacement
réversible dans `.corbeille/`).

### PDF PsychonautWiki (rendu)

La page est récupérée via l'API MediaWiki puis convertie en PDF par le moteur
Qt (`QTextDocument` + `QPdfWriter`) :

* les scripts, liens d'édition (« modifier ») et contenus masqués
  (`display:none`) sont retirés — Qt ignore cette propriété et les
  imprimerait sinon ;
* une feuille de style d'impression dédiée est appliquée : titres,
  tableaux et encarts d'interaction PsychonautWiki, références, notes de
  page ;
* un en-tête porte la source, le titre, le lien de la page et la date
  d'export ;
* les **figures sont intégrées au PDF** : les images de la page sont
  téléchargées (une fois par URL, SVG compris) puis embarquées dans le
  document — Qt ne pouvant pas accéder au réseau lui-même, elles seraient
  sinon absentes.

### Lecteur de documents

Le double-clic (ou *Ouvrir dans le lecteur*) ouvre dans des onglets :

* **PDF** — pages, zoom, recherche dans le texte (`QPdfDocument` + `QPdfView`) ;
* **texte** (`txt`, `md`, `csv`, `json`, `bib`, `log`…) — lecture seule en
  monospace, recherche avec retour à la ligne ;
* **HTML** — avec les images et feuilles de style locales ;
* **images** (PNG, JPEG, GIF…) — affichage avec barres de défilement ;
* **EPUB** — converti en HTML autonome dans un thread de fond ;
* tout autre format part vers l'application par défaut du système.

Chaque onglet, quel que soit son type, affiche à droite le résumé de la
molécule de son dossier. Le volet droit suit aussi la **sélection** dans
l'explorateur : pas besoin d'ouvrir un fichier pour consulter le résumé de sa
molécule — mais tant qu'un document est ouvert, c'est sa molécule qui
s'affiche (voir plus haut : jamais de résumé « à côté » pendant une lecture).

### Choix des résultats ambigus

Le choix se fait **dans l'interface** : une fenêtre liste les candidats
(PsychonautWiki puis Wikipédia) et la tâche reprend avec le titre retenu.
Aucun titre n'est deviné, jamais dans le terminal.

### Recherche d'articles (PubMed) et de livres

Dialogue *Rechercher un article ou un livre…*, avec sélecteur de source :

| Source | Contenu |
|--------|---------|
| **PubMed** | références indexées (E-utilities du NCBI, puis enrichissement Europe PMC) |
| **PubMed Central** | articles en accès libre (E-utilities) |
| **Internet Archive** | téléchargement direct seulement si l'élément est signalé **domaine public** |
| **Open Library** | métadonnées et accès en ligne |

Un **double-clic sur un résultat** tente le téléchargement du PDF ; s'il n'y a
pas de PDF libre, la page de la référence s'ouvre dans le navigateur.

Recherche du PDF, dans cet ordre :

1. liens PDF renvoyés par Europe PMC ;
2. `oa_url` et `locations` d'**OpenAlex** (par DOI, puis par PMID) ;
3. **Semantic Scholar** (`openAccessPdf`, API ouverte sans clé — une seconde
   tentative suit un refus de débit) ;
4. **Unpaywall** si une adresse de contact est connue : variable
   `UNPAYWALL_EMAIL` ou champ « E-mail (Unpaywall) » du dialogue, conservé
   localement via `QSettings` ;
5. en dernier, les liens `?pdf=render` d'Europe PMC (souvent bloqués hors
   navigateur).

Chaque URL est testée et validée (`%PDF`), avec les en-têtes d'un navigateur
classique (langue, `Accept`, `Referer` de la page de l'article).

**Repli texte intégral.** Quand tous les PDF d'éditeurs refusent les robots
(403 très fréquent), l'API **Europe PMC** livre quand même le texte intégral
JATS des articles présents dans PMC, en accès libre : le logiciel le
convertit en HTML puis en PDF localement (titre, résumé, sections, tableaux,
références). Le bouton de téléchargement est donc tentable pour tout article
PubMed ; si rien ne réussit, un message clair propose *Ouvrir la page*.

Aucun contenu piraté n'est téléchargé : seuls les PDF et textes réellement
en accès libre sont récupérés — ni Sci-Hub, ni Z-Library, ni autre contournement
de paywall.

### Page « Comparaison »

La fenêtre comporte trois onglets permanents en haut :

* **Recherche** — les trois volets habituels (explorateur, lecteur, résumé) ;
* **Comparaison** — l'explorateur + un tableau comparatif remplaçant le
  lecteur et le volet résumé : **1 ligne par molécule**, colonnes =
  `COMPARE_ORDER` lues dans les `resume.json`, dans cet ordre : **nom**,
  **état** (disque : disponible, absente, en file…), classification
  chimique, classification psychoactive, dosage, puis les données
  pharmacocinétiques (demi-vie, biodisponibilité, liaison protéique /
  *protein binding*, métabolisme, métabolites, onset, come-up, Tmax / peak,
  offset), pour finir par la **toxicité**. Les autres champs du tableau
  unifié (`TARGET_ORDER` : voies, durée totale, addiction, tolérance,
  tolérance croisée, effets, interactions…) restent présents dans les
  `resume.json` et alimentent notamment les interactions locales, mais ne
  sont pas affichés en colonnes. Pour chaque cellule, la valeur fusionnée
  `pharmacology` est lue en premier, puis les champs bruts PsychonautWiki,
  puis l'infobox Wikipedia (utile quand un `resume.json` a été généré sans
  section `pharmacology`). Pour la **demi-vie**, une valeur fusionnée qui ne
  commence pas par une durée (phrase citant le métabolite) est remplacée par
  la ligne d'infobox, plus structurée ;
* **Interactions** — le croisement des interactions entre plusieurs
  molécules (voir plus bas).

Le même explorateur sert aux trois pages : il est déplacé automatiquement
d'un onglet à l'autre. **Toute ouverture de document ramène à l'onglet
Recherche** (le lecteur n'existe que sur cette page) ; à la fermeture, la
page Recherche est également rétablie avant de mémoriser la disposition
des volets.

* **Ajouts** : « Ajouter une molécule… » dans la barre d'outils de la page
  (plusieurs noms acceptés d'un coup, séparés par `;`), ou clic droit dans
  l'explorateur → « Ajouter à la comparaison » (le dossier de la molécule
  est résolu automatiquement, insensible à la casse et aux accents).
* **États par ligne** : *en file*, *téléchargement…*, *disponible*,
  *échec*, *absente*. Un double-clic sur une ligne ouvre son
  `resume.html` (et bascule sur Recherche).
* **Téléchargement** : une molécule absente est mise en file dès son ajout ;
  à chaque ouverture de la page, la liste est relue depuis les préférences
  et **les molécules manquantes sont téléchargées automatiquement** — une à
  la fois, avec la même construction que le bouton « + » (dossier existant
  sans résumé ⇒ simple régénération du `resume.json`). Le bouton
  « Télécharger les absentes » relance les absentes et les échecs ; les
  échecs de la file n'ouvrent pas de fenêtre, l'état de la ligne suffit.
* **Confort** : colonnes masquables (menu « Colonnes »), tri par clic sur
  les en-têtes, infobulle avec la valeur complète quand une cellule est
  tronquée, liste de molécules et colonnes visibles conservées au
  redémarrage (`QSettings`).
* **Gestes de la table** : la touche `Suppr` retire la ou les lignes
  sélectionnées ; on peut **glisser une molécule depuis l'explorateur et la
  déposer dans le tableau** (dossier ou fichier : la molécule est résolue
  automatiquement) ; enfin, le **glisser d'une ou plusieurs lignes du
  tableau** les réordonne à la position de dépôt — le tri en cours est alors
  effacé (l'ordre manuel prime) mais reste réutilisable par un nouveau clic
  sur un en-tête. L'ordre est conservé au redémarrage.
* « Vider » ne supprime que le tableau : aucun dossier du répertoire de
  travail n'est touché.

### Page « Interactions »

Troisième onglet permanent : on y saisit **plusieurs molécules** et l'outil
rend, pour chaque paire, l'interaction documentée par les sources. Aucun
conseil médical n'est produit : il s'agit d'un simple rapprochement de
données.

Trois sources, du plus local au plus distant :

1. **PsychonautWiki (données locales)** — le champ « interactions » déjà
   présent dans chaque `resume.json` du répertoire de travail. Aucun accès
   réseau ; la détection se fait par nom de molécule en mot entier,
   insensible à la casse et aux accents. C'est un rapprochement
   approximatif (PsychonautWiki cite parfois des classes entières, ex.
   « MAOIs »), signalé comme tel.
2. **DDInter** — base ouverte (CC BY-NC-SA 4.0, <https://ddinter.scbdd.com/>),
   téléchargée **une seule fois** puis interrogée hors ligne. 14 fichiers CSV
   (~31 Mo), 2 300 médicaments, plus de 300 000 paires classées
   `Minor`/`Moderate`/`Major`. Stockage : `~/.cache/pharma/ddinter/`
   (ou `$XDG_CACHE_HOME/pharma/ddinter`). Bouton **« Télécharger la base
   DDInter »** dans la barre d'outils. Couverture à connaître : les
   molécules récréatives classiques (LSD, MDMA, psilocybine, cocaïne,
   méthamphétamine) sont **absentes** ; kétamine, nicotine, caféine,
   éthanol, amphétamine, cannabidiol, benzodiazépines, ISRS et opioïdes
   sont présents. Les molécules hors base sont listées dans la barre d'état.
3. **Wikidata** — propriété *significant drug interaction* (P769), licence
   CC0. Requête SPARQL ponctuelle, résultat mis en cache 30 jours
   (`~/.cache/pharma/ddinter/wikidata_p769.json`). Décochable ; en cas
   d'indisponibilité (par exemple limitation de débit HTTP 429) les sources
   locales restent affichées et un message le signale.

Fonctionnement :

* **Liste des molécules** à gauche : ajout par « Ajouter une molécule… »
  (autocomplétion sur les molécules du répertoire et sur la base DDInter ;
  plusieurs noms acceptés d'un coup, séparés par `;`),
  par **glisser depuis l'explorateur** (dossier ou fichier, résolu
  automatiquement), ou par clic droit dans l'explorateur → « Ajouter aux
  interactions ». Les dépôts venus d'autres applications sont ignorés.
* **Tableau des paires** à droite : une ligne par paire, avec
  `Molécule A` · `Molécule B` · `Niveau` · `Mécanisme` · `Source` · `Détail`.
  Les niveaux sont colorés ; le tri par l'en-tête `Niveau` suit la gravité
  (`Contre-indiqué` > `Majeur` > `Modéré` > `Mineur` > `Mentionné` >
  `Aucune`). Un filtre par niveau et une case **« Masquer les paires sans
  interaction »** restreignent l'affichage ; les cellules tronquées portent
  une infobulle complète.
* **Colonne « Mécanisme »** : classe chaque paire selon un mécanisme
  **probable** — *Synergie* (deux dépresseurs du SNC, deux stimulants),
  *Potentialisation* (IMAO associé à une substance sérotoninergique ou
  sympathomimétique), *Addition / cumul* (deux substances sérotoninergiques,
  charge anticholinergique), *Antagonisme* (antagoniste opioïde, antipsychotique
  face à un agoniste), *Métabolisme* (inhibiteur ou inducteur CYP) et
  *Toxicité* (QT, hépatotoxicité, néphrotoxicité, risque hémorragique). Le
  classement est **déduit** du nom et des classes chimique/psychoactive
  PsychonautWiki (`resume.json`), sans aucune donnée supplémentaire : c'est une
  aide au repérage, **approximative et non médicale**. L'infobulle de la cellule
  donne le détail du raisonnement ; si rien n'est déductible, la valeur
  *Non précisé* est affichée avec les traits retenus. Toute paire peut être
  fixée à la main dans `mechanisms.json` (voir ci-dessous).
* **« Reprendre la comparaison »** recopie les molécules de la page
  Comparaison dans la page Interactions (sans doublon).
* **« Exporter CSV… »** écrit les paires affichées (séparateur `;`, BOM
  UTF-8 : lisible directement par Excel francophone).
* La liste des molécules est conservée au redémarrage (`QSettings`) ; la
  base DDInter n'est jamais téléchargée sans action explicite.

### Page « Calendrier »

Un **journal personnel des prises** : molécule, date et heure, dose (texte
libre). L'outil n'enregistre que ce que l'utilisateur saisit — **aucune
posologie n'est proposée** — et représente la **période d'effet estimée**
après chaque prise.

* **Trois niveaux de vue**, comme un agenda : **Année** (12 mini-mois),
  **Mois** (grille des jours) et **Jour** (frise horizontale 0 h–24 h avec
  **une ligne par prise**). Aux trois niveaux, chaque molécule est
  **représentée par une ligne** : dans les mini-journées de l'année et dans
  les cases du mois, ces lignes sont tracées sur un axe **0 h → 24 h**
  (progression de gauche à droite dans la case), une ligne par molécule,
  toujours au même rang — si bien qu'une prise qui dépasse minuit **continue
  la même ligne** dans la case du lendemain. On passe de l'un
  à l'autre en cliquant un jour ou le nom d'un mois, ou par la liste
  *Vue :* ; les flèches `◀ ▶` reculent ou avancent d'une année, d'un mois ou
  d'un jour selon le niveau, et *Aujourd'hui* revient à la date du jour.
* **« Ajouter une prise… »** demande la molécule (autocomplétion sur les
  molécules du travail et les noms déjà saisis), la date, l'heure, la dose
  (texte libre) et une note facultative. Dans la vue Jour, **double-cliquer
  une prise** (liste ou ligne de la frise) ouvre sa **courbe
  pharmacocinétique** (page suivante) ; le menu contextuel permet de la
  **modifier** ou de la **supprimer**, comme le bouton *Modifier…*.
* **Sélectionner une prise** : cliquer sur une **ligne** (vue Jour)
  sélectionne la prise correspondante dans la liste — et réciproquement,
  choisir une ligne de la liste surligne la ligne de la frise. Les boutons
  *Modifier…* et *Supprimer* sont alors actifs, et la touche `Suppr` agit
  directement dans la liste. Si le jour ne compte **qu'une seule prise**,
  elle est visée sans clic préalable.
* **Période sous influence** : chaque prise a sa propre **ligne**
  (lignes parallèles, ordonnées par heure de prise), de sorte que plusieurs
  molécules sont représentées le même jour sans se superposer. La période
  est dessinée **en segments de cette ligne** — et non en blocs — selon les
  phases estimées à partir des **durées documentées** de la molécule lues
  dans `resume.json` : `onset` (temps d'apparition, « latence »), `comeup`
  (« montée »), `peak` (« plateau ») et `offset` (« descente »).
  Exemple : pour le LSD, une prise à 21 h 30 couvre environ 12 h, réparties
  en ces quatre phases. Le champ *Durée d'effet* du dialogue permet de
  saisir une autre durée, sinon « auto » utilise ces données ; une prise
  dont aucune durée n'est documentée reste représentée par son point seul,
  avec la mention *durée inconnue*.
* **Ligne d'élimination** : quand la demi-vie est documentée, la ligne se
  **prolonge** au-delà des phases (en gris clair) jusqu'à **5 demi-vies**,
  convention courante d'une élimination quasi complète (≈ 97 %). C'est une
  **estimation pharmacocinétique**, distincte de la période d'effet : elle
  figure dans la légende et dans les infobulles.
* **Repères** : un **point plein** marque l'instant de la prise et la
  demi-vie documentée un **unique point creux** marqué `t½` (plus de
  multiples `2 t½`, `3 t½`… : c'est la ligne qui va jusqu'à 5 t½). La
  demi-vie est lue dans
  `pharmacology.halflife` du `resume.json` : seul le premier nombre suivi
  de son unité est retenu, car les phrases de Wikipedia en citent souvent
  d'autres ensuite (temps jusqu'au pic…). Elle apparaît aussi dans les
  libellés (`t½ 2.9 h`) et les infobulles de la vue Jour ; aux échelles
  **Année** et **Mois**, le même point creux est tracé dans la case du jour
  où tombe la demi-vie, aligné sur la ligne de la molécule (il apparaît donc
  dans la case du lendemain quand la prise a lieu le soir).
* Une prise dont l'effet dépasse minuit **teinte légèrement le lendemain**
  dans les vues Année et Mois (le fond n'est qu'un pâle rappel de la phase
  dominante : ce sont les lignes qui portent l'information) ; la vue Jour
  reprend la suite de la ligne avec le sous-titre *suite*.
* Le journal est un fichier JSON versionné `calendrier.json`, écrit **à la
  racine du répertoire de travail** (il accompagne donc les molécules) ;
  lisible et éditable à la main. Tant qu'aucun répertoire n'est ouvert, le
  fichier global `~/.local/share/pharma/calendrier.json` sert de repli, et il
  est repris automatiquement la première fois qu'un journal de travail est
  créé. Le bouton *Actualiser* relit le fichier et les durées documentées.
* **Enregistrer / Charger / Effacer** : le menu *Fichier* propose
  « Enregistrer le calendrier… » (`Ctrl+Alt+S`), « Charger un calendrier… »
  (`Ctrl+Alt+O`) et « Effacer le calendrier… » ; les mêmes actions sont dans
  la barre d'outils du calendrier (*Enregistrer…*, *Charger…*, *Effacer…*).
  Le chargement **remplace** le journal — une confirmation est demandée s'il
  n'est pas vide — à partir d'un fichier au format `pharma-calendrier` (une
  simple liste d'entrées est aussi acceptée) ; l'effacement vide le journal et
  son fichier par défaut, après confirmation. Un fichier absent, illisible ou
  d'un format inconnu produit un message clair sans rien modifier.
* **Glisser-déposer** : déposer un dossier ou fichier de molécule de
  l'explorateur sur le calendrier ouvre le dialogue de saisie pré-rempli.

> La période affichée est une **estimation documentaire** (durées issues de
> PsychonautWiki / Wikipedia) : ce n'est ni une donnée médicale, ni une
> indication de conduite à tenir.

### Page « Pharmacocinétique »

Cinquième page : elle trace la **courbe d'évolution de la concentration
relative** d'une molécule après une prise, à partir de sa dose et de son
`resume.json`. **Aucune posologie n'y est proposée** : c'est une
**estimation documentaire**, jamais une donnée médicale.

* On saisit une **molécule** (autocomplétion sur les molécules du travail et
  le journal) et une **dose** (texte libre, par ex. `150 µg`, `1,5 g`,
  `100 mg`), puis **Tracer**. Seul le premier nombre suivi de son unité sert
  d'échelle ; une dose non chiffrée donne une courbe normalisée (sommet à 1).
* **Modèle** : un compartiment, absorption puis élimination du premier ordre
  (fonction de Bateman). La constante d'élimination est `ke = ln 2 / demi-vie`
  et la constante d'absorption `ka` est résolue pour que le sommet tombe sur
  le **Tmax**, pris comme la fin de la montée (`onset + come-up`) documentée.
  La biodisponibilité chiffrée, quand elle existe, met la courbe à l'échelle.
* **Repères** : la **verticale pointillée** marque le Tmax et l'unique
  **point creux** la demi-vie (`t½`) ; l'axe horizontal est le temps depuis la
  prise en heures — **en jours au-delà de deux jours** (demi-vies longues,
  par ex. la fluoxétine) — et l'axe vertical une **concentration relative** en
  unités arbitraires (`u.a.`). La durée tracée est de **5 demi-vies**.
* **Demi-vie** : lue dans le champ fusionné `pharmacology.halflife`. Quand
  celui-ci **ne commence pas par une durée** — extraction ayant glissé le
  chiffre du métabolite (« norfluoxetine is longer (16 days)… ») — la ligne
  « Elimination half-life » de l'infobox Wikipédia, structurée, fait foi. Le
  panneau affiche la plage documentée, la valeur **médiane retenue pour le
  modèle** et le **texte source** (« 1–3 days (acute) 4–6 days (chronic) »).
* Le volume de distribution n'étant pas documenté, l'échelle verticale reste
  **relative** : elle est proportionnelle à la dose saisie pour une **même**
  molécule, mais **n'est pas comparable entre molécules**.
* **Superposition** : tracer une dose différente ou une autre molécule ajoute
  une courbe (une couleur par courbe, légende en haut à droite) ; retracer la
  même molécule à la même dose la remplace. *Vider* efface le graphique.
* **Sélection et mise en évidence** : choisir une ligne de la liste
  **met en évidence** sa courbe (trait épais, tracés des autres atténués, ses
  repères Tmax et `t½` affichés) ; **cliquer une courbe** du graphique
  sélectionne à son tour sa ligne dans la liste.
* **Afficher / masquer** : la **case à cocher** de chaque ligne (ou le bouton
  *Afficher / masquer*, ou le menu contextuel) affiche ou masque une courbe
  sans la retirer ; son libellé est grisé quand elle est masquée, et les
  échelles du graphique se recalculent sur les courbes visibles.
* **Supprimer une courbe** : le bouton ***Retirer***, la touche **Suppr**
  (ou Retour arrière) ou le menu contextuel enlèvent la courbe sélectionnée.
* **Échelle logarithmique** : la case *Échelle logarithmique (axe vertical)*
  étale les fins de courbe (élimination lente), que l'axe linéaire écrase.
* Le panneau de gauche rappelle les **paramètres utilisés** (demi-vie, Tmax
  et sa source, constantes, biodisponibilité, durée) ; une molécule sans
  demi-vie documentée donne un message clair au lieu d'une courbe.
* **Depuis le calendrier** : double-cliquer une prise (liste du jour ou ligne
  de la frise) bascule sur cette page et affiche la courbe correspondant à la
  molécule, la dose et la date de la prise. Le bouton ***Courbes du jour…***
  (ou le menu contextuel d'une prise) **superpose toutes les prises d'une même
  journée** en une seule fois. On peut aussi déposer un dossier de molécule de
  l'explorateur sur la page.

### Enregistrer et charger les tableaux

Les tableaux **Comparaison** et **Interactions** peuvent être conservés dans
des fichiers JSON, en plus de leur mémorisation automatique (`QSettings`) :

* **Menu Fichier** : « Enregistrer les tableaux… » (`Ctrl+Shift+S`) écrit les
  *deux* tableaux dans un seul fichier ; « Charger les tableaux… »
  (`Ctrl+Shift+O`) les restaure.
* **Barre d'outils de chaque page** : « Enregistrer… » / « Charger… » font de
  même (les deux tableaux), et **« Enregistrer ce tableau… » / « Charger dans
  ce tableau… »** n'agissent que sur la page courante — le fichier ne contient
  alors que la section correspondante (comparaison *ou* interactions), l'autre
  restant intacte au chargement.
* Le fichier (format `pharma-tableaux`, versionné) contient la liste des
  molécules de chaque tableau **dans l'ordre affiché**, le dossier associé de
  la comparaison, les colonnes masquées et le répertoire d'origine (informatif).
  Il est lisible et éditable à la main, et peut être versionné ou partagé.
* Au chargement, la comparaison est **réévaluée sur le disque** (l'état de
  chaque ligne : disponible, absente, en file…) et les molécules absentes
  remises en file de téléchargement ; la page Interactions est recalculée à
  partir de ses trois sources. Seul le tableau est restauré : aucun fichier du
  répertoire de travail n'est supprimé.
* Un fichier absent, illisible ou d'un format inconnu produit un message
  clair sans rien modifier.

### Recherche, annulation et apparence

* **Filtre par onglet** : chaque page de données possède un champ de recherche
  qui **n'affiche que les lignes correspondantes** — *Comparaison* (toutes
  colonnes), *Interactions* (molécules, mécanisme, sources), *Calendrier*
  (molécule : la frise et la liste du jour se limitent aux prises filtrées) et
  *Pharmacocinétique* (la liste des courbes ; le graphique conserve toutes les
  courbes tracées). La comparaison se fait **sans accents ni casse**.
* **Annuler / rétablir** : `Ctrl+Z` annule, `Ctrl+Y` (ou `Ctrl+Shift+Z`)
  rétablit, sur les quatre pages de données (ajout/retrait de molécules,
  ordre des lignes, noms d'interactions, prises du calendrier, courbes
  pharmacocinétiques). Le menu **Édition** rappelle ces deux actions, grisées
  quand il n'y a rien à faire ; l'historique (60 étapes) est remis à zéro à
  l'ouverture d'un répertoire de travail.
* **Répertoires récents** : le menu **Fichier → Répertoires récents** (10 max)
  rouvre directement un dossier déjà utilisé, et propose de vider la liste.
* **Thème sombre** : le menu **Affichage → Thème sombre** (case à cocher) bascule
  l'ensemble de l'interface — palette et graphiques compris — entre clair et
  sombre ; le choix est mémorisé.
* **Éditeur de synonymes** : le menu **Données → Éditeur de synonymes** (ou le
  bouton de la page Interactions) ouvre la table d'alias franco-anglaise utilisée
  pour rapprocher les noms de la base DDInter, et l'enregistre dans
  `~/.cache/pharma/ddinter/synonyms.json`.

### Raccourcis et confort

* `Ctrl+O` répertoire de travail · `Ctrl+F` rechercher dans le document ·
  `Ctrl+L` rechercher un article ou un livre · `Ctrl+M` ajouter une molécule ·
  `Ctrl+W` fermer l'onglet · `Ctrl+1` page Recherche ·
  `Ctrl+2` page Comparaison · `Ctrl+3` page Interactions ·
  `Ctrl+4` page Calendrier · `Ctrl+5` page Pharmacocinétique ·
  `Ctrl+Z` annuler · `Ctrl+Y` rétablir ·
  `Ctrl+Shift+S` enregistrer les tableaux · `Ctrl+Shift+O` charger les tableaux ·
  `Ctrl+Alt+S` enregistrer le calendrier · `Ctrl+Alt+O` charger le calendrier.
* Le volet droit affiche `resume.html` **déjà généré** : instantané, sans
  accès réseau. Le bouton *Régénérer le résumé* relance le parser sur le
  dossier de la molécule affichée (même chose que le menu contextuel de
  l'explorateur).
* La touche `Suppr` place dans `.corbeille` le fichier ou dossier sélectionné
  dans l'explorateur (confirmation demandée), et retire les molécules
  sélectionnées de la page Interactions.
* Les téléchargements, les conversions et le parsing s'exécutent dans des
  threads de fond (barre de progression en bas) : l'interface ne se fige pas.
* Si le PDF Wikipédia échoue, le résumé est tout de même écrit dans le dossier.
* Fenêtres, colonnes et répertoire de travail sont mémorisés (`QSettings`).
  À l'ouverture, le **dernier répertoire de travail** est rouvert et les
  **deux tableaux** comme le **calendrier** de la session précédente sont
  rechargés automatiquement, même s'ils n'ont pas été enregistrés à la main :
  la comparaison est réévaluée sur le disque, les interactions recalculées à
  partir de leurs sources et le journal des prises relu depuis son fichier.

Rappel : application de **recherche documentaire**, ni avis médical, ni conseil
de dosage.

## Fichiers du projet

* `recherche_molecule.py` : coeur du projet — clients GraphQL/MediaWiki, parseurs,
  fusion des sources, lignes du tableau unifié, export JSON/Markdown, CLI.
* `studio_backend.py` : logique de l'atelier sans Qt (chemins sûrs, téléchargement
  des PDF Wikipédia, génération des résumés, recherche PubMed / littérature,
  import et conversion des documents, mise en page d'impression des pages
  HTML, conversion du texte intégral Europe PMC en PDF, enregistrement et
  chargement des tableaux en JSON).
* `pharma_studio.py` : interface de bureau PySide6 (3 volets, lecteur de
  documents, dialogues d'ajout, pages « Comparaison » avec son tableau et sa
  file de téléchargement, « Interactions », « Calendrier » et
  « Pharmacocinétique »).
* `requirements.txt` : dépendances (`requests`, `wikipedia`, `beautifulsoup4`,
  `PySide6`).
* `README.md` : ce fichier.

Données hors projet (jamais dans le répertoire de travail) :

* `~/.cache/pharma/ddinter/` : base DDInter téléchargée (14 CSV) et cache
  Wikidata. Un fichier `synonyms.json` peut y être ajouté pour compléter la
  table d'alias franco-anglaise, par exemple
  `{"Bupropion LP": "Bupropion", "Sertraline (Zoloft)": "Sertraline"}`.
  Un fichier `mechanisms.json` peut y être ajouté pour **fixer** le mécanisme
  affiché d'une paire, l'heuristique étant alors ignorée, par exemple
  `{"lsd+sertraline": {"label": "Antagonisme", "note": "effet ressenti
  émoussé"}}` (clé = les deux noms normalisés, sans accents et minuscules,
  triés et joints par `+`).

## Mises en garde importantes

* Ce projet est destiné à la **recherche documentaire uniquement**. Ce n'est
  pas un outil de dosage ni un conseil médical.
* Les données proviennent de wikis communautaires (PsychonautWiki,
  Wikipedia) et peuvent contenir des erreurs. Toujours recouper avec des
  sources primaires (publications scientifiques, notices, bases de données
  telles que DrugBank / PubChem) avant toute utilisation.
* La page « Interactions » ne fait que **rapprocher des données
  documentaires** : elle n'évalue ni la conduite à tenir, ni les posologies,
  ni les contre-indications. Les sources ont des licences propres (DDInter :
  CC BY-NC-SA 4.0 ; Wikidata : CC0) et des couvertures partielles — l'absence
  de paire ne signifie pas l'absence d'interaction. Vérifier auprès d'une
  source de référence.
* L'extraction automatique est imperfecte : certains champs peuvent être
  absents ou approximatifs selon la page (le wikitext brut est archivé dans
  le JSON exporté).

## Licence

Ce projet est fourni à titre éducatif, sans garantie.
