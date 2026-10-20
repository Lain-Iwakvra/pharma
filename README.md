# Pharma Studio — 1.0

**Atelier de recherche psychopharmacologique documentaire.**

Pharma Studio rassemble, pour une molécule donnée, les informations
pharmacologiques publiées par des sources libres (PsychonautWiki, Wikipédia,
Wikidata, DDInter) et les présente dans une application de bureau : un
explorateur de fichiers, un lecteur de documents, un tableau comparatif, un
calcul d'interactions, un calendrier de prises et un tracé pharmacocinétique.

> ⚠️ **Recherche documentaire uniquement.** Aucun avis médical, aucune
> posologie, aucune conduite à tenir. Les données proviennent de wikis
> communautaires et peuvent contenir des erreurs : recoupez toujours avec des
> sources primaires (publications, notices, DrugBank, PubChem…).

---

## Sommaire

1. [Aperçu](#aperçu)
2. [Installation](#installation)
3. [Ligne de commande](#ligne-de-commande)
4. [Application de bureau](#application-de-bureau)
   - [Le répertoire de travail](#le-répertoire-de-travail)
   - [Explorateur et lecteur](#explorateur-et-lecteur)
   - [Page *Comparaison*](#page-comparaison)
   - [Page *Interactions*](#page-interactions)
   - [Page *Calendrier*](#page-calendrier)
   - [Page *Pharmacocinétique*](#page-pharmacocinétique)
5. [Enregistrer, charger, exporter](#enregistrer-charger-exporter)
6. [Recherche, annulation et apparence](#recherche-annulation-et-apparence)
7. [Raccourcis clavier](#raccourcis-clavier)
8. [Fichiers et formats](#fichiers-et-formats)
9. [Nouveautés de la 1.0](#nouveautés-de-la-10)
10. [Mises en garde et licence](#mises-en-garde-et-licence)

---

## Aperçu

Pour une molécule, le **cœur de parsing** (`recherche_molecule.py`) :

1. interroge l'**API GraphQL Bifrost de PsychonautWiki**
   (`https://api.psychonautwiki.org/`) — classification, noms communs,
   potentiel addictif, toxicité, tolérance, interactions, effets, et par voie
   d'administration (ROA) : dosage, durée d'apparition, *come-up*, pic,
   *offset*, biodisponibilité ;
2. récupère la **page MediaWiki de PsychonautWiki** (wikitext) pour compléter
   les champs absents de GraphQL, notamment la **demi-vie** (*half-life*) ;
3. récupère la **page Wikipédia en anglais** (API MediaWiki, via la librairie
   `wikipedia` si présente, sinon appel direct) pour le statut légal, la
   toxicité détaillée, la chimie, et les lignes du tableau
   *Pharmacokinetic data* de l'infobox : liaison protéique
   (*protein binding*), métabolisme, métabolites, demi-vie, biodisponibilité ;
4. **fusionne** le tout en un rapport unique : dosage, voie(s), *onset*,
   *Tmax / peak*, demi-vie, biodisponibilité, durée d'action, effets résiduels,
   toxicité, potentiel addictif, statut légal.

**PsychonautWiki fait foi** (wiki spécialisé), Wikipédia complète les manques.
Chaque information n'apparaît qu'une fois, sans étiquette de provenance.

L'approche s'inspire de
[pw-cli](https://github.com/pharmacologic/pw-cli), qui interroge le même API
GraphQL Bifrost.

Deux façons de s'en servir, **sur le même cœur de parsing** :

| Interface | Fichier | Usage |
|-----------|---------|-------|
| Ligne de commande | `recherche_molecule.py` | recherche ponctuelle, export JSON / Markdown |
| Application de bureau | `pharma_studio.py` | atelier à volets : fichiers, documents, résumé, comparaison, interactions, calendrier, pharmacocinétique |

---

## Installation

```bash
cd /svr/link/projet/pharma
pip install -r requirements.txt
```

Dépendances : `requests`, `wikipedia`, `beautifulsoup4`, `PySide6 >= 6.6`
(PySide6 fournit QtPdf, le rendu HTML → PDF et les dialogues).

> Si l'environnement bloque l'installation (PEP 668), utilisez
> `pip install --break-system-packages -r requirements.txt`, ou créez un
> environnement virtuel :
> `python3 -m venv .venv && source .venv/bin/activate`.

---

## Ligne de commande

```bash
# Affichage seul
python3 recherche_molecule.py LSD

# Avec sauvegarde du rapport
python3 recherche_molecule.py "3-MMC" --save json --save md
python3 recherche_molecule.py MDMA --save md
```

| Option | Effet |
|--------|-------|
| `molecule` | nom à rechercher (positionnel, requis) |
| `--save json` / `--save md` | enregistre `recherche_<molécule>.json` et/ou `.md` (répétable) |

Quand plusieurs substances correspondent au nom saisi et qu'aucun titre exact
n'existe, **le choix n'est jamais silencieux** :

* **terminal interactif** → menu numéroté ;
* **terminal non interactif** (flux redirigé, cron…) → la liste des candidats
  est affichée et le script s'arrête avec le code de sortie `1` ;
* **application de bureau** → une fenêtre liste les candidats et la tâche
  reprend avec le titre retenu (jamais de question dans un terminal).

### Exemple de sortie

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

---

## Application de bureau

```bash
python3 pharma_studio.py                    # demande le répertoire de travail
python3 pharma_studio.py --workspace ~/pharma
```

Au premier lancement, une boîte de dialogue demande le **répertoire de
travail** ; il est mémorisé pour les fois suivantes (`QSettings`).

La fenêtre comporte **cinq pages permanentes**, en haut (`Ctrl+1` à `Ctrl+5`) :

| Page | Contenu |
|------|---------|
| **Recherche** | explorateur + lecteur de documents + résumé de la molécule |
| **Comparaison** | tableau : 1 ligne par molécule, colonnes pharmacologiques |
| **Interactions** | croisement des interactions de plusieurs molécules |
| **Calendrier** | journal des prises et période d'effet estimée |
| **Pharmacocinétique** | courbe de concentration relative estimée |

```
┌────────────────┬──────────────────────────┬──────────────────────┐
│  Explorateur   │  Lecteur de documents    │   Résumé (parser)    │
│ (racine +      │  PDF · texte · HTML ·    │  tableau unifié      │
│  fichiers)     │  images · EPUB · onglets │  (resume.html)       │
└────────────────┴──────────────────────────┴──────────────────────┘
```

### Le répertoire de travail

Le **répertoire de travail lui-même** est la racine de l'arborescence (les
dossiers voisins sont masqués) ; toutes les actions du bouton « + » s'y
appliquent aussi.

```text
<racine>/
  calendrier.json               <- journal des prises (voir Calendrier)
  LSD/                          <- dossier de molécule
    wikipedia_LSD.pdf           <- PDF de la page Wikipédia anglaise
    psychonautwiki_LSD.pdf      <- PDF de la page PsychonautWiki
    resume.html                 <- version affichée dans le volet droit
    resume.json                 <- données brutes (mêmes que la CLI)
    etudes/
      Hofmann 1943.pdf          <- hérite de la molécule « LSD »
  .corbeille/                   <- corbeille interne (suppressions réversibles)
```

Le dossier d'une molécule ne contient que l'essentiel (deux PDF, deux
résumés) : `resume.md` n'est plus généré (un fichier hérité est retiré à la
régénération) et `.pharma.json` n'est créé que par une association
**explicite**.

**À quelle molécule appartient un document ?** Au **dossier ancêtre le plus
proche** (sous la racine) qui « ressemble » à une molécule : ancêtres examinés
du plus proche au plus loin, `.pharma.json` d'abord puis `resume.*`. Un
dossier générique (« notes », « etudes », « 2024 »…) n'est jamais deviné être
une molécule.

**La racine n'est jamais une molécule**, même si elle porte un `.pharma.json`
ou un `resume.*` : sans cette règle, une association posée sur la racine par
erreur ferait accroire que tout le travail appartient à une seule molécule.
Le menu contextuel de la racine n'offre donc ni *Associer à une molécule…* ni
*Régénérer le résumé*.

Le lien dossier ↔ molécule se pose à la main : clic droit sur un dossier →
*Associer à une molécule…* ; **laisser le champ vide retire l'association**.
Le titre du volet résumé affiche le **nom déclaré dans `resume.json`** quand
il diffère du nom de dossier, avec la mention `— dossier « … »`.

Le résumé se met à jour **automatiquement** :

* tant qu'un **document est ouvert**, c'est **sa** molécule qui commande (une
  sélection accidentelle dans l'explorateur ne remplace jamais le résumé du
  document lu) ;
* sans document ouvert, une **simple sélection** de fichier ou dossier de
  molécule affiche déjà son résumé ;
* ouvrir un document — dans un onglet ou avec l'application du système —
  resynchronise le volet ;
* si `resume.html` manque alors que `resume.json` existe, le HTML est
  **reconstruit localement**, hors ligne ;
* si la molécule n'a aucun résumé **et que le dossier lui est explicitement
  associé** (`.pharma.json`), il est **récupéré en tâche de fond** (une seule
  tentative par dossier et par session).

### Explorateur et lecteur

Le bouton « + » (barre d'outils ou clic droit), disponible sur la racine comme
sur chaque dossier :

| Action | Effet |
|--------|-------|
| **Créer un sous-dossier…** | nouveau dossier libre |
| **Ajouter une molécule…** | crée `<Molécule>/` avec le PDF Wikipédia, le PDF PsychonautWiki (rendu depuis la page récupérée, figures intégrées) et `resume.html` / `resume.json`. Plusieurs noms peuvent être saisis d'un coup, séparés par `;` |
| **Ajouter le PDF d'une page Wikipédia…** | télécharge le PDF de la page dont vous donnez le titre |
| **Ajouter le PDF d'une page PsychonautWiki…** | récupère la page PsychonautWiki et la rend en PDF (figures intégrées, feuille de style d'impression) |
| **Ajouter un document existant…** | importe un PDF, texte, image, HTML ou EPUB du disque |
| **Rechercher un article ou un livre…** | cherche dans les sources **libres** et importe le document obtenu (PDF, EPUB ou texte) |

Le clic droit sur un dossier propose aussi *Régénérer le résumé* (relance le
parser) et *Placer dans la corbeille* (déplacement réversible dans
`.corbeille/`).

**Rendu PDF PsychonautWiki.** La page est récupérée via l'API MediaWiki puis
convertie par le moteur Qt (`QTextDocument` + `QPdfWriter`) : scripts, liens
d'édition et contenus masqués retirés ; feuille de style d'impression dédiée
(titres, tableaux, encarts d'interaction, références) ; en-tête avec source,
titre, lien et date ; **figures téléchargées une fois par URL (SVG compris)
et intégrées** au PDF.

**Lecteur de documents.** Le double-clic (ou *Ouvrir dans le lecteur*) ouvre
dans des onglets :

| Type | Prise en charge |
|------|-----------------|
| **PDF** | pages, zoom, recherche dans le texte (QtPdf) |
| **texte** (`txt`, `md`, `csv`, `json`, `bib`, `log`…) | lecture seule en monospace, recherche |
| **HTML** | images et feuilles de style locales |
| **images** (PNG, JPEG, GIF…) | affichage avec barres de défilement |
| **EPUB** | converti en HTML autonome dans un thread de fond |
| autre | ouvert par l'application par défaut du système |

**Recherche d'articles (PubMed) et de livres.** Le dialogue *Rechercher un
article ou un livre…* propose plusieurs sources :

| Source | Contenu |
|--------|---------|
| **PubMed** | références indexées (E-utilities du NCBI, enrichissement Europe PMC) |
| **PubMed Central** | articles en accès libre (E-utilities, **tri par pertinence** — le tri par défaut de PMC est la date de publication) |
| **Internet Archive** | téléchargement seulement si l'élément est **domaine public** — le nom réel du fichier est résolu au moment du téléchargement via l'API `metadata` (les scans sont en `{id}_djvu.pdf`, les dépôts ont des noms arbitraires) |
| **Open Library** | métadonnées et accès en ligne ; le téléchargement n'est proposé que pour les ouvrages en accès **public**, les emprunts et les fichiers sans version numérique restant des liens de consultation |

Par défaut, le choix **« Toutes les sources »** interroge les quatre sources
**en parallèle** : la panne d'une source n'interrompt pas les autres et son
message s'affiche sous le tableau, avec le nombre de lignes apportées par
chaque source. Les **doublons** d'une même référence sont fusionnés en une
ligne — PubMed et PubMed Central publient le même article, Internet Archive
héberge souvent le même livre qu'Open Library — la provenance restant lisible
dans la colonne *Source* (`PubMed + PubMed Central`). L'identification passe
par le DOI, puis le PMID/PMCID, puis le titre normalisé (avec année) ; les
en-têtes `tool`/`email` demandés par le NCBI sont envoyés, les appels sont
cadencés et repris après un refus de débit (`429`), ce qui autorise les deux
recherches E-utilities simultanées.

Un **double-clic sur un résultat** tente le téléchargement du document (PDF,
EPUB ou texte, selon ce que contient réellement l'élément) ; à défaut,
la page de la référence s'ouvre dans le navigateur. Pour les **articles**,
recherche du PDF, dans cet ordre :

1. liens PDF d'**Europe PMC** ;
2. `oa_url` et `locations` d'**OpenAlex** (par DOI, puis PMID) ;
3. **Semantic Scholar** (`openAccessPdf`, sans clé ; seconde tentative après
   un refus de débit) ;
4. **Unpaywall** si une adresse de contact est connue — variable
   `UNPAYWALL_EMAIL` ou champ *E-mail (Unpaywall)* du dialogue, conservé
   localement ;
5. les liens `?pdf=render` d'Europe PMC (souvent bloqués hors navigateur).

Chaque URL est testée et validée (signature `%PDF` pour les PDF) avec les
en-têtes d'un navigateur classique. **Repli texte intégral** : quand les PDF
d'éditeurs refusent les
robots (403 fréquent), l'API **Europe PMC** livre le texte JATS des articles
en accès libre, converti localement en HTML puis PDF (titre, résumé,
sections, tableaux, références).

Aucun contenu piraté n'est téléchargé : ni Sci-Hub, ni Z-Library, ni autre
contournement de paywall.

### Page *Comparaison*

L'explorateur + un tableau comparatif (qui remplace lecteur et résumé) :
**1 ligne par molécule**, colonnes lues dans les `resume.json`, dans cet
ordre : **nom**, **état** (disque : disponible, absente, en file…),
classification chimique, classification psychoactive, dosage, puis les données
pharmacocinétiques (**demi-vie**, biodisponibilité, liaison protéique, *protein
binding*, métabolisme, métabolites, *onset*, *come-up*, *Tmax / peak*,
*offset*), pour finir par la **toxicité**.

Les autres champs du tableau unifié (*TARGET_ORDER* : voies, durée totale,
addiction, tolérance, effets, interactions…) restent dans les `resume.json`
et alimentent les interactions locales, sans être affichés en colonnes.

Pour chaque cellule, la valeur fusionnée `pharmacology` est lue d'abord, puis
les champs bruts PsychonautWiki, puis l'infobox Wikipédia. Pour la
**demi-vie**, une valeur fusionnée qui ne commence pas par une durée (phrase
citant un métabolite) est remplacée par la ligne d'infobox, plus structurée.

* **Ajouter** : « Ajouter une molécule… » (plusieurs noms séparés par `;`), ou
  clic droit dans l'explorateur → *Ajouter à la comparaison* (dossier résolu
  automatiquement, insensible à la casse et aux accents).
* **États** : *en file*, *téléchargement…*, *disponible*, *échec*, *absente*.
  Un double-clic sur une ligne ouvre son `resume.html` (et revient à
  Recherche).
* **Téléchargement** : une molécule absente est mise en file dès son ajout ; à
  chaque ouverture de la page, les manquantes sont téléchargées
  automatiquement, une à une. « Télécharger les absentes » relance absentes et
  échecs.
* **Confort** : colonnes masquables (menu *Colonnes*), tri par clic sur les
  en-têtes, infobulle quand une cellule est tronquée ; liste et colonnes
  conservées au redémarrage.
* **Gestes** : `Suppr` retire les lignes sélectionnées ; on peut **glisser une
  molécule** depuis l'explorateur dans le tableau, et **glisser les lignes du
  tableau** pour les réordonner (le tri en cours est alors effacé, l'ordre
  manuel prime).
* « Vider » ne supprime que le tableau : aucun dossier n'est touché.

### Page *Interactions*

On y saisit **plusieurs molécules** ; l'outil rend, pour chaque paire,
l'interaction documentée par les sources. **Aucun conseil médical** : c'est un
rapprochement de données.

**Trois sources**, du plus local au plus distant :

1. **PsychonautWiki (données locales)** — le champ *interactions* déjà présent
   dans chaque `resume.json`. Aucun accès réseau ; détection par nom en mot
   entier, insensible à la casse et aux accents. Rapprochement **approximatif**
   (PsychonautWiki cite parfois des classes entières, ex. « MAOIs »).
2. **DDInter** — base ouverte (CC BY-NC-SA 4.0,
   <https://ddinter.scbdd.com/>) téléchargée **une seule fois** puis
   interrogée hors ligne : 14 fichiers CSV (~31 Mo), ~2 300 médicaments, plus
   de 300 000 paires classées *Minor* / *Moderate* / *Major*. Stockage :
   `~/.cache/pharma/ddinter/`. Bouton **« Télécharger la base DDInter »**.
   Couverture partielle : LSD, MDMA, psilocybine, cocaïne ou méthamphétamine
   sont **absentes** ; kétamine, nicotine, caféine, éthanol, amphétamine,
   cannabidiol, benzodiazépines, ISRS et opioïdes sont présents. Les molécules
   hors base sont listées dans la barre d'état.
3. **Wikidata** — propriété *significant drug interaction* (P769), licence
   CC0. Requête SPARQL ponctuelle, résultat mis en cache 30 jours. Décochable ;
   en cas d'indisponibilité (HTTP 429…), les sources locales restent
   affichées et un message le signale.

**Fonctionnement :**

* **Liste des molécules** à gauche : « Ajouter une molécule… »
  (autocomplétion sur les molécules du répertoire et la base DDInter, plusieurs
  noms séparés par `;`), **glisser depuis l'explorateur**, ou clic droit →
  *Ajouter aux interactions*. Les dépôts venus d'autres applications sont
  ignorés.
* **Tableau des paires** à droite : `Molécule A` · `Molécule B` · `Niveau` ·
  `Mécanisme` · `Source` · `Détail`. Niveaux colorés ; tri par gravité
  (*Contre-indiqué* > *Majeur* > *Modéré* > *Mineur* > *Mentionné* >
  *Aucune*). Filtre par niveau et case *Masquer les paires sans interaction*.
* **Colonne *Mécanisme*** : classe la paire selon un mécanisme **probable** —
  *Synergie* (deux dépresseurs du SNC, deux stimulants), *Potentialisation*
  (IMAO + sérotoninergique ou sympathomimétique), *Addition / cumul* (deux
  sérotoninergiques, charge anticholinergique), *Antagonisme* (antagoniste
  opioïde, antipsychotique face à un agoniste), *Métabolisme* (inhibiteur ou
  inducteur CYP), *Toxicité* (QT, hépatotoxicité, néphrotoxicité, risque
  hémorragique). Le classement est **déduit** des noms et des classes
  PsychonautWiki : aide au repérage, **approximative et non médicale**.
  L'infobulle donne le raisonnement ; une paire peut être fixée à la main dans
  `mechanisms.json`.
* **« Reprendre la comparaison »** recopie les molécules de la page
  Comparaison (sans doublon).
* **« Exporter CSV… »** écrit les paires affichées (séparateur `;`, BOM
  UTF-8).
* La liste des molécules est conservée au redémarrage ; la base DDInter n'est
  jamais téléchargée sans action explicite.

### Page *Calendrier*

Un **journal personnel des prises** : molécule, date et heure, dose (texte
libre). L'outil n'enregistre que ce que vous saisissez — **aucune posologie
n'est proposée** — et représente la **période d'effet estimée** après chaque
prise.

* **Trois niveaux de vue**, comme un agenda : **Année** (12 mini-mois),
  **Mois** (grille des jours) et **Jour** (frise horizontale 0 h – 24 h, **une
  ligne par prise**). Aux trois niveaux, chaque molécule est **représentée par
  une ligne** tracée sur un axe 0 h → 24 h, toujours au même rang : une prise
  qui dépasse minuit **continue la même ligne** dans la case du lendemain. On
  passe d'un niveau à l'autre en cliquant un jour ou le nom d'un mois, ou par
  la liste *Vue :* ; les flèches `◀ ▶` reculent ou avancent d'une année, d'un
  mois ou d'un jour, et *Aujourd'hui* revient à la date du jour.
* **« Ajouter une prise… »** demande la molécule (autocomplétion), la date,
  l'heure, la dose (texte libre) et une note facultative. Dans la vue Jour,
  **double-cliquer une prise** (liste ou ligne) ouvre sa **courbe
  pharmacocinétique** ; le menu contextuel permet de la **modifier** ou de la
  **supprimer**.
* **Sélectionner une prise** : cliquer une **ligne** de la frise sélectionne la
  prise dans la liste, et réciproquement. *Modifier…* et *Supprimer* sont
  alors actifs, et `Suppr` agit directement dans la liste. Si le jour ne
  compte **qu'une seule prise**, elle est visée sans clic préalable.
* **Période sous influence** : chaque prise a sa propre ligne (lignes
  parallèles, ordonnées par heure), dessinée **en segments** selon les phases
  estimées à partir des **durées documentées** du `resume.json` : `onset`
  (« latence »), `comeup` (« montée »), `peak` (« plateau ») et `offset`
  (« descente »). Le champ *Durée d'effet* du dialogue permet de saisir une
  autre durée ; « auto » utilise les données. Une prise sans durée documentée
  reste représentée par son point seul, avec la mention *durée inconnue*.
* **Ligne d'élimination** : quand la demi-vie est documentée, la ligne se
  **prolonge** au-delà des phases (en gris clair) jusqu'à **5 demi-vies**
  (≈ 97 %, convention d'élimination quasi complète). C'est une **estimation
  pharmacocinétique**, distincte de la période d'effet.
* **Repères** : un **point plein** marque l'instant de la prise et la
  demi-vie un **unique point creux `t½`** (la ligne va jusqu'à 5 t½). La
  demi-vie est lue dans `pharmacology.halflife` : seul le premier nombre suivi
  de son unité est retenu. Aux échelles Année et Mois, le point `t½` est tracé
  dans la case du jour où il tombe, aligné sur la ligne de la molécule.
* Une prise dont l'effet dépasse minuit **teinte légèrement le lendemain**
  dans les vues Année et Mois (rappel pâle de la phase dominante) ; la vue Jour
  reprend la suite de la ligne avec le sous-titre *suite*.
* **Journal** : fichier JSON versionné `calendrier.json`, écrit **à la racine
  du répertoire de travail**, lisible et éditable à la main. Sans répertoire
  ouvert, `~/.local/share/pharma/calendrier.json` sert de repli et est repris
  automatiquement à la première création d'un journal de travail. *Actualiser*
  relit le fichier et les durées.
* **Enregistrer / Charger / Effacer** : menu *Fichier* → « Enregistrer le
  calendrier… » (`Ctrl+Alt+S`), « Charger un calendrier… » (`Ctrl+Alt+O`),
  « Effacer le calendrier… » ; mêmes actions dans la barre d'outils. Le
  chargement **remplace** le journal (confirmation si non vide) depuis un
  fichier au format `pharma-calendrier` (une simple liste d'entrées est aussi
  acceptée) ; l'effacement vide le journal et son fichier par défaut, après
  confirmation.
* **Glisser-déposer** : déposer un dossier ou fichier de molécule de
  l'explorateur sur le calendrier ouvre le dialogue pré-rempli.
* **Statistiques…** (barre d'outils ou menu *Données*) : tableau **par
  molécule** — nombre de prises, première et dernière date, intervalle moyen,
  **dose cumulée** (doses chiffrables converties en mg) et **durée d'effet
  cumulée** estimée. Statistiques descriptives de vos propres saisies.

> La période affichée est une **estimation documentaire** (durées issues de
> PsychonautWiki / Wikipédia) : ce n'est ni une donnée médicale, ni une
> indication de conduite à tenir.

### Page *Pharmacocinétique*

Cette page trace la **courbe d'évolution de la concentration relative** d'une
molécule après une prise, à partir de sa dose et de son `resume.json`.
**Aucune posologie n'y est proposée** : c'est une **estimation documentaire**.

* On saisit une **molécule** (autocomplétion) et une **dose** (texte libre :
  `150 µg`, `1,5 g`, `100 mg`…), puis **Tracer**. Seul le premier nombre suivi
  de son unité sert d'échelle ; une dose non chiffrée donne une courbe
  normalisée (sommet à 1).
* **Modèle** : un compartiment, absorption puis élimination du premier ordre
  (fonction de Bateman). `ke = ln 2 / demi-vie` ; `ka` est résolu pour que le
  sommet tombe sur le **Tmax** documenté (fin de la montée `onset + come-up`).
  La biodisponibilité chiffrée, quand elle existe, met la courbe à l'échelle.
* **Repères** : une **verticale pointillée** au Tmax, un **point creux** à la
  demi-vie (`t½`). L'axe horizontal est le temps depuis la prise en heures —
  **en jours au-delà de deux jours** (demi-vies longues, ex. fluoxétine) — et
  l'axe vertical une **concentration relative** en unités arbitraires (`u.a.`).
  La durée tracée est de **5 demi-vies**.
* **Demi-vie** : lue dans `pharmacology.halflife`. Quand celui-ci **ne
  commence pas par une durée** (extraction ayant glissé le chiffre du
  métabolite, ex. « norfluoxetine is longer (16 days)… »), la ligne
  *Elimination half-life* de l'infobox Wikipédia fait foi. Le panneau affiche
  la plage documentée, la **valeur médiane retenue** et le **texte source**.
* L'échelle verticale reste **relative** (volume de distribution non
  documenté) : proportionnelle à la dose pour une **même** molécule, mais
  **non comparable entre molécules**.
* **Superposition** : tracer une autre dose ou molécule ajoute une courbe
  (une couleur par courbe, légende en haut à droite) ; retracer la même
  molécule à la même dose la remplace. *Vider* efface le graphique.
* **Sélection** : choisir une ligne **met en évidence** sa courbe (trait
  épais, autres atténuées, repères affichés) ; **cliquer une courbe** du
  graphique sélectionne sa ligne dans la liste.
* **Afficher / masquer** : la case de chaque ligne (ou *Afficher / masquer*,
  ou le menu contextuel) masque une courbe sans la retirer ; les échelles se
  recalculent sur les courbes visibles.
* **Supprimer** : bouton *Retirer*, touche `Suppr` / Retour arrière, ou menu
  contextuel.
* **Échelle logarithmique** : la case *Échelle logarithmique (axe vertical)*
  étale les fins de courbe (élimination lente), que l'axe linéaire écrase.
* Le panneau gauche rappelle les **paramètres utilisés** (demi-vie, Tmax et sa
  source, constantes, biodisponibilité, durée) ; une molécule sans demi-vie
  documentée donne un message clair au lieu d'une courbe.
* **Depuis le calendrier** : double-cliquer une prise bascule sur cette page et
  affiche la courbe correspondante (molécule, dose, date). Le bouton *Courbes
  du jour…* (ou le menu contextuel d'une prise) **superpose toutes les prises
  d'une même journée**. On peut aussi déposer un dossier de molécule sur la
  page.

---

## Enregistrer, charger, exporter

### Tableaux

Les tableaux **Comparaison** et **Interactions** se conservent en JSON, en plus
de la mémorisation automatique :

* **Menu Fichier** : « Enregistrer les tableaux… » (`Ctrl+Shift+S`) écrit les
  *deux* tableaux dans un seul fichier ; « Charger les tableaux… »
  (`Ctrl+Shift+O`) les restaure.
* **Barre d'outils de chaque page** : *Enregistrer…* / *Charger…* font de même
  (les deux tableaux) ; *Enregistrer ce tableau…* / *Charger dans ce
  tableau…* n'agissent que sur la page courante — le fichier ne contient alors
  que la section correspondante, l'autre restant intacte au chargement.
* Le fichier (format **`pharma-tableaux`**, versionné) contient la liste des
  molécules de chaque tableau **dans l'ordre affiché**, le dossier associé de
  la comparaison, les colonnes masquées et le répertoire d'origine
  (informatif). Lisible et éditable à la main, il peut être versionné ou
  partagé.
* Au chargement, la comparaison est **réévaluée sur le disque** (disponible,
  absente, en file…) et les absentes remises en file ; la page Interactions est
  recalculée depuis ses trois sources. Seul le tableau est restauré : aucun
  fichier du répertoire de travail n'est supprimé.
* Un fichier absent, illisible ou d'un format inconnu produit un message clair
  sans rien modifier.

### Exports par page

Chaque page de données propose un export adapté, depuis sa barre d'outils :

| Page | Export | Contenu |
|------|--------|---------|
| **Comparaison** | *Exporter CSV…* | **lignes affichées** (filtre compris) avec les **colonnes visibles**, séparateur `;`, BOM UTF-8 |
| **Interactions** | *Exporter CSV…* | paires affichées (A, B, niveau, mécanisme, sources, détail) |
| **Calendrier** | *Exporter CSV…* | journal des prises (molécule, début, dose, durée, note) |
| **Calendrier** | *Exporter iCal…* | fichier `.ics` (un `VEVENT` par prise, durée d'effet estimée) |
| **Pharmacocinétique** | *Exporter PNG…* | le graphique courant (courbes visibles, échelle comprise) |

Les exports CSV / iCal / PNG n'écrivent que des fichiers choisis par
l'utilisateur ; aucune donnée du répertoire de travail n'est modifiée.

---

## Recherche, annulation et apparence

* **Filtre par onglet** — chaque page de données possède un champ de recherche
  qui **n'affiche que les lignes correspondantes** : *Comparaison* (toutes
  colonnes), *Interactions* (molécules, mécanisme, sources), *Calendrier*
  (molécule : frise et liste du jour limitées aux prises filtrées) et
  *Pharmacocinétique* (la liste des courbes ; le graphique garde toutes les
  courbes tracées). La comparaison ignore accents et casse.
* **Annuler / rétablir** — `Ctrl+Z` annule, `Ctrl+Y` (ou `Ctrl+Shift+Z`)
  rétablit, sur les quatre pages de données (ajout/retrait de molécules, ordre
  des lignes, noms d'interactions, prises, courbes). Le menu **Édition** grise
  ces actions quand il n'y a rien à faire ; l'historique (60 étapes) est remis
  à zéro à l'ouverture d'un répertoire.
* **Répertoires récents** — menu **Fichier → Répertoires récents** (10 max)
  rouvre un dossier déjà utilisé, et propose de vider la liste.
* **Thème clair / sombre** — menu **Affichage → Thème sombre** : bascule toute
  l'interface (palette et graphiques compris) ; le choix est mémorisé. Les deux
  thèmes partagent une **feuille de style unique** : onglets soulignés d'un
  liseré d'accent, boutons et champs à bords arrondis avec état de survol,
  en-têtes de tableaux discrets, barres de défilement fines. Les vues peintes à
  la main (frise du calendrier, courbes) gardent leur rendu propre.
* **Éditeur de synonymes** — menu **Données → Éditeur de synonymes** (ou bouton
  de la page Interactions) : table d'alias franco-anglaise utilisée pour
  rapprocher les noms de la base DDInter, enregistrée dans
  `~/.cache/pharma/ddinter/synonyms.json`.

---

## Raccourcis clavier

| Raccourci | Action |
|-----------|--------|
| `Ctrl+O` | ouvrir un répertoire de travail |
| `Ctrl+F` | rechercher dans le document |
| `Ctrl+L` | rechercher un article ou un livre |
| `Ctrl+M` | ajouter une molécule |
| `Ctrl+W` | fermer l'onglet |
| `Ctrl+1` … `Ctrl+5` | pages Recherche, Comparaison, Interactions, Calendrier, Pharmacocinétique |
| `Ctrl+Z` / `Ctrl+Y` (`Ctrl+Shift+Z`) | annuler / rétablir |
| `Ctrl+Shift+S` / `Ctrl+Shift+O` | enregistrer / charger les tableaux |
| `Ctrl+Alt+S` / `Ctrl+Alt+O` | enregistrer / charger le calendrier |
| `Suppr` | corbeille (explorateur), retrait de lignes, de prises ou de courbes |
| `Retour arrière` | retirer une courbe (page Pharmacocinétique) |

---

## Fichiers et formats

### Projet

| Fichier | Rôle |
|---------|------|
| `recherche_molecule.py` | cœur : clients GraphQL / MediaWiki, parseurs, fusion des sources, lignes du tableau unifié, export JSON / Markdown, CLI |
| `studio_backend.py` | logique de l'atelier sans Qt : chemins sûrs, téléchargements, génération des résumés, recherche PubMed / littérature, import et conversion de documents, mise en page d'impression, texte intégral Europe PMC → PDF, tableaux JSON, **modèle pharmacocinétique** et statistiques du journal |
| `pharma_studio.py` | interface PySide6 : 3 volets, lecteur, dialogues, 5 pages, thème, historique, filtres, exports |
| `requirements.txt` | dépendances (`requests`, `wikipedia`, `beautifulsoup4`, `PySide6`) |
| `README.md` | ce document |

### Dans le répertoire de travail

| Fichier | Contenu |
|---------|---------|
| `resume.json` | données brutes d'une molécule (mêmes que la CLI) |
| `resume.html` | résumé mis en forme, affiché dans le volet droit |
| `calendrier.json` | journal des prises (format `pharma-calendrier`) |
| `.pharma.json` | association explicite d'un dossier à une molécule |

### Hors projet (jamais dans le répertoire de travail)

* `~/.cache/pharma/ddinter/` — base DDInter téléchargée (14 CSV) et cache
  Wikidata.
  * `synonyms.json` (facultatif) — complète la table d'alias franco-anglaise,
    par ex. `{"Bupropion LP": "Bupropion", "Sertraline (Zoloft)": "Sertraline"}`.
  * `mechanisms.json` (facultatif) — **fixe** le mécanisme affiché d'une paire
    (l'heuristique est alors ignorée), par ex.
    `{"lsd+sertraline": {"label": "Antagonisme", "note": "effet ressenti émoussé"}}`
    (clé = les deux noms normalisés, sans accents et en minuscules, triés et
    joints par `+`).
* `~/.local/share/pharma/calendrier.json` — journal global de repli, tant
  qu'aucun répertoire de travail n'est ouvert.

---

## Nouveautés de la 1.0

Première version stable, qui rassemble :

* **cœur de parsing partagé** PsychonautWiki + Wikipédia (fusion, infobox
  *Pharmacokinetic data*, demi-vie structurée) et exports JSON / Markdown ;
* **application de bureau** PySide6 à **cinq pages** — Recherche, Comparaison,
  Interactions, Calendrier, Pharmacocinétique ;
* **interactions** à trois sources (PsychonautWiki local, base DDInter hors
  ligne, Wikidata P769) avec détection de mécanisme et export CSV ;
* **calendrier** à trois échelles (année / mois / jour), période d'effet
  estimée, ligne d'élimination à 5 demi-vies, statistiques et export CSV / iCal ;
* **pharmacocinétique** (modèle à un compartiment de Bateman), superposition,
  échelle logarithmique et export PNG ;
* **confort** : filtre par onglet, annuler / rétablir, répertoires récents,
  éditeur de synonymes, thème clair / sombre et **mise en forme soignée
  commune aux deux thèmes** ;
* **imports et recherche** : PDF Wikipédia / PsychonautWiki, documents locaux,
  recherche PubMed / Internet Archive / Open Library avec récupération des PDF
  en accès libre.

---

## Mises en garde et licence

* Ce projet est destiné à la **recherche documentaire uniquement**. Ce n'est
  **pas** un outil de dosage ni un conseil médical.
* Les données proviennent de wikis communautaires (PsychonautWiki, Wikipédia)
  et peuvent contenir des erreurs. Recoupez toujours avec des sources
  primaires (publications scientifiques, notices, DrugBank, PubChem).
* La page *Interactions* ne fait que **rapprocher des données documentaires** :
  elle n'évalue ni la conduite à tenir, ni les posologies, ni les
  contre-indications. Les sources ont leurs propres licences (DDInter :
  CC BY-NC-SA 4.0 ; Wikidata : CC0) et leurs couvertures partielles — l'absence
  d'une paire ne signifie pas l'absence d'interaction.
* L'extraction automatique est imparfaite : certains champs peuvent être
  absents ou approximatifs selon la page (le wikitext brut est archivé dans le
  JSON exporté).

**Licence** — projet fourni à titre éducatif, sans garantie.
