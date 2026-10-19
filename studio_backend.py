#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
studio_backend.py
-----------------

Coeur de l'application de bureau : tout ce qui touche au disque et au reseau,
**sans aucune dependance a Qt**. L'interface (pharma_studio.py) se contente
d'appeler ces fonctions depuis des threads de travail, ce qui rend ce module
testable en ligne de commande.

Organisation d'un repertoire de travail :

    <racine>/
      LSD/
        wikipedia_LSD.pdf         <- PDF de la page Wikipedia
        psychonautwiki_LSD.pdf    <- PDF de la page PsychonautWiki
        resume.html               <- resume pret a afficher (volet droit)
        resume.json               <- donnees brutes (parser)
        etudes/                   <- sous-dossiers libres
          article.pdf

Toutes les operations de fichiers passent par `safe_path()` : impossible de
sortir de la racine du repertoire de travail, meme avec un chemin tordu.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import math
import os
import re
import shutil
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape as esc_html
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote, urljoin

import requests

import recherche_molecule as rm

try:
    from bs4 import BeautifulSoup  # nettoyage HTML avant impression
except Exception:  # pragma: no cover - dependance declaree dans requirements
    BeautifulSoup = None

# --- constantes --------------------------------------------------------------
MOLECULE_META = ".pharma.json"          # metadonnees d'un dossier de molecule
RESUME_MD = "resume.md"                 # ancien format, conserve en lecture
RESUME_JSON = "resume.json"
RESUME_HTML = "resume.html"             # version prete a afficher (volet droit)
PDF_PREFIX = "wikipedia_"               # prefixe des PDF telecharges
PW_PDF_PREFIX = "psychonautwiki_"       # prefixe des PDF de pages PsychonautWiki

REST_API = "https://en.wikipedia.org/api/rest_v1"
PMC_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PUBMED_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EPMC_REST = "https://www.ebi.ac.uk/europepmc/webservices/rest"
OPENALEX_API = "https://api.openalex.org"
UNPAYWALL_API = "https://api.unpaywall.org/v2"
SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1/paper"
IA_SEARCH = "https://archive.org/advancedsearch.php"
OL_SEARCH = "https://openlibrary.org/search.json"

# Unpaywall exige une adresse de contact pour l'API gratuite :
# variable d'environnement UNPAYWALL_EMAIL, ou champ « e-mail » du dialogue
# de recherche bibliographique (conserve localement via QSettings).
UNPAYWALL_EMAIL = os.environ.get("UNPAYWALL_EMAIL", "")

HTTP_TIMEOUT = 60
Progress = Optional[Callable[[str], None]]
Fraction = Optional[Callable[[float], None]]


class WorkspaceError(Exception):
    """Erreur d'acces disque dans le repertoire de travail."""


class AcquireError(Exception):
    """Echec d'une acquisition (telechargement, recherche, import)."""


# --- chemins ----------------------------------------------------------------
def is_inside(root: Path, path: Path) -> bool:
    """Vrai si `path` est `root` lui-meme ou se trouve sous `root`."""
    root = Path(root).resolve()
    path = Path(path).resolve()
    return path == root or root in path.parents


def safe_path(root: Path, *parts: str) -> Path:
    """Construit un chemin sous `root` en refusant toute evasion.

    Leve WorkspaceError si le chemin resolu sort du repertoire de travail.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise WorkspaceError(f"Repertoire de travail introuvable : {root}")
    candidate = root
    for part in parts:
        candidate = candidate / str(part)
    resolved = candidate.resolve()
    if not is_inside(root, resolved):
        raise WorkspaceError(
            f"Chemin refuse (hors du repertoire de travail) : {candidate}"
        )
    return resolved


def unique_path(path: Path) -> Path:
    """Retourne `path`, ou `path (2)`, `path (3)...` s'il existe deja."""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    i = 2
    while True:
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def slugify(name: str, fallback: str = "sans_nom") -> str:
    """Nom de fichier / dossier sur : accents retires, caracteres neutres.

    On garde les lettres, les chiffres, l'espace, le tiret, le souligne et le
    point ; tout le reste devient un tiret. Les accents sont translitteres.
    """
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s.\-]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text or fallback


# --- metadonnees d'un dossier de molecule ------------------------------------
def read_molecule_meta(directory: Path) -> str:
    """Molecule declaree dans `<directory>/.pharma.json`, sinon chaine vide."""
    meta = Path(directory) / MOLECULE_META
    if not meta.is_file():
        return ""
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("molecule", "") or "")


def write_molecule_meta(directory: Path, molecule: str) -> None:
    """Declare explicitement la molecule d'un dossier."""
    meta = Path(directory) / MOLECULE_META
    payload = {"molecule": molecule}
    meta.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def clear_molecule_meta(directory: Path) -> None:
    """Retire l'association explicite d'un dossier (supprime `.pharma.json`).

    N'echoue pas si le fichier n'existe pas deja.
    """
    meta = Path(directory) / MOLECULE_META
    try:
        meta.unlink()
    except FileNotFoundError:
        pass


def ancestor_dirs(path: Path, root: Path) -> list[Path]:
    """Dossiers ancetres de `path` sous `root`, du plus proche au plus loin."""
    root = Path(root).resolve()
    out: list[Path] = []
    cur = Path(path).resolve().parent
    while cur == root or root in cur.parents:
        out.append(cur)
        if cur == root:
            break
        cur = cur.parent
    return out


def is_workspace_root(path: Path, root: Optional[Path]) -> bool:
    """Vrai si `path` designe la racine du repertoire de travail.

    La racine n'est **jamais** une molecule : cette question revient partout
    ou un chemin choisi par l'utilisateur pourrait la designer (selection,
    menu contextuel, association). Chemins resoles pour ne pas confondre un
    dossier et un lien symbolique qui y mene.
    """
    if root is None or path is None:
        return False
    try:
        return Path(path).resolve() == Path(root).resolve()
    except OSError:
        return Path(path) == Path(root)


def locate_molecule(path: Path, root: Path) -> tuple[Optional[Path], str]:
    """Dossier de molecule et nom associes a un document : (dir, molecule).

    Les dossiers ancetres sont examines **du plus proche au plus loin** et le
    premier signe de molecule gagne, dans l'ordre pour un dossier donne :
    1. metadonnee explicite `.pharma.json` ;
    2. resume du parser (`resume.json`, `resume.html` ou `resume.md`).

    La racine du repertoire de travail est exclue : meme porteur d'un
    `.pharma.json` ou d'un resume, elle n'est jamais une molecule — sinon une
    association posee sur la racine ferait croire que **tout** le travail
    appartient a une seule molecule (tous les documents s'y resolvaient).

    Un dossier generique sans aucun signe (« etudes », « 2024 »…) n'est pas
    devine molecule non plus. Sinon (None, "").
    """
    root = Path(root).resolve()

    for directory in ancestor_dirs(path, root):
        if directory == root:
            continue
        declared = read_molecule_meta(directory)
        if declared:
            return directory, declared
        if (directory / RESUME_JSON).is_file() or (
            directory / RESUME_HTML
        ).is_file() or (directory / RESUME_MD).is_file():
            return directory, directory.name

    return None, ""


def find_molecule_for(path: Path, root: Path) -> str:
    """Nom de la molecule associee a un document (voir locate_molecule)."""
    return locate_molecule(path, root)[1]


def molecule_catalogue(root: Path) -> list[dict]:
    """Inventaire des molecules du travail (vues classees de l'explorateur).

    Explore recursivement `root` a la recherche des dossiers contenant un
    `resume.json` — les dossiers caches, dont `.corbeille`, sont ignores —
    et en lit les classifications PsychonautWiki. Chaque entree donne le
    nom, le chemin du dossier, les classes chimique et psychoactive et la
    liste des effets rapportes. Un resume illisible est simplement ignore.
    """
    root = Path(root)
    out: list[dict] = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if RESUME_JSON not in filenames:
            continue
        directory = Path(dirpath)
        if directory == root:
            # la racine n'est jamais une molecule (voir locate_molecule) :
            # son resume ne doit pas fabriquer de fausse entree dans ces
            # inventaires (doublon, dossier = racine, donnee perimee).
            continue
        try:
            data = json.loads(
                (directory / RESUME_JSON).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        pw = data.get("psychonautwiki_graphql")
        if not isinstance(pw, dict):
            pw = {}
        entry = {
            "name": str(data.get("molecule") or directory.name),
            "path": str(directory),
        }
        for key in ("chemical_class", "psychoactive_class", "effects"):
            values = pw.get(key)
            if isinstance(values, str):
                values = [values]
            entry[key] = [
                str(value).strip() for value in (values or [])
                if str(value).strip()
            ]
        # tolérance croisée peut venir sous plusieurs formes selon sérialisation
        cross_vals = []
        for cand in ("cross_tolerances", "crossTolerance", "cross_tolerance"):
            v = pw.get(cand)
            if isinstance(v, list):
                cross_vals.extend(v)
            elif isinstance(v, str) and v.strip():
                cross_vals.append(v.strip())
        # also check pharmacology section? sometimes info is duplicated
        if not cross_vals and isinstance(data.get("pharmacology"), dict):
            pharm = data["pharmacology"]
            if isinstance(pharm.get("cross_tolerance"), dict):
                cv = pharm["cross_tolerance"].get("value") or pharm["cross_tolerance"].get("pw") or pharm["cross_tolerance"].get("wiki")
                if isinstance(cv, str) and cv.strip():
                    # split by ; ,
                    import re
                    cross_vals = [x.strip() for x in re.split(r'[;,]', cv) if x.strip()]
        if not cross_vals and isinstance(data.get("cross_tolerance"), str) and data.get("cross_tolerance"):
            import re
            cross_vals = [x.strip() for x in re.split(r'[;,]', str(data["cross_tolerance"])) if x.strip()]
        entry["drug_class"] = list(entry.get("psychoactive_class") or [])
        entry["cross_tolerance"] = [str(x).strip() for x in cross_vals if str(x).strip()]
        out.append(entry)
    return out


# --- comparaison de molecules ------------------------------------------------
def _flatten_value(value) -> str:
    """Valeur de champ comparable : liste -> « a ; b », autre -> str()."""
    if isinstance(value, list):
        return "; ".join(str(v).strip() for v in value if str(v).strip())
    if value is None:
        return ""
    return str(value).strip()


# Libelles d'infobox Wikipedia -> cles de champs pharmacologiques. Sert de
# dernier repli quand le resume n'a pas encore ces champs fusionnes (fichiers
# generes avant l'extraction, ou generation partielle) : les donnees sont
# pourtant presentes dans `wikipedia_infobox`.
_WIKI_FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "halflife": ("half-life", "half life", "halflife"),
    "bioavailability": ("bioavailability",),
    "protein_binding": ("protein binding", "plasma protein binding"),
    "metabolism": ("metabolism",),
    "metabolites": ("metabolites",),
}


def _wiki_infobox_values(data: dict) -> dict[str, str]:
    """{libelle normalise: valeur} de l'infobox Wikipedia d'un resume.

    Regroupe les champs plats (`wikipedia_infobox["fields"]`) et les lignes
    des encadres en sections (`wikipedia_infobox["sections"]`, ex.
    « Pharmacokinetic data ») sous forme de paires libelle/valeur.
    """
    infobox = data.get("wikipedia_infobox")
    if not isinstance(infobox, dict):
        return {}
    out: dict[str, str] = {}

    def _add(label, value) -> None:
        text = _flatten_value(value)
        name = rm.fold_accents(str(label or "")).casefold().strip()
        if name and text:
            out.setdefault(name, text)

    fields = infobox.get("fields")
    if isinstance(fields, dict):
        for label, value in fields.items():
            _add(label, value)
    sections = infobox.get("sections")
    if isinstance(sections, dict):
        for rows in sections.values():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, (list, tuple)) and len(row) == 2:
                    _add(row[0], row[1])
    return out


def _wiki_value(values: dict[str, str], key: str) -> str:
    """Valeur d'infobox correspondant a une cle de champ, ou « »."""
    for label in _WIKI_FIELD_LABELS.get(key, ()):
        needle = rm.fold_accents(label).casefold()
        for candidate, value in values.items():
            if needle in candidate:
                return value
    return ""


def comparison_rows(root: Path) -> list[dict]:
    """Une entree par molecule du travail, pour le tableau de comparaison.

    Champs = `rm.TARGET_ORDER` (hors « name ») et `rm.COMPARE_ORDER` lus dans
    `resume.json` : la valeur fusionnee `pharmacology[champ]["value"]`
    d'abord, avec repli sur les champs bruts PsychonautWiki (classes, effets,
    voies...), puis sur l'infobox Wikipedia (demi-vie, liaison proteique,
    metabolisme...). `TARGET_ORDER` reste couvert en entier car le champ
    « interactions » sert aux interactions locales, meme s'il n'est plus
    affiche en colonne.
    Chaque entree : {"name", "path", "fields": {champ: texte}}. Un resume
    illisible est ignore.
    """
    root = Path(root)
    out: list[dict] = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if RESUME_JSON not in filenames:
            continue
        directory = Path(dirpath)
        if directory == root:
            # la racine n'est jamais une molecule (voir locate_molecule) :
            # son resume ne doit pas fabriquer de fausse entree dans ces
            # inventaires (doublon, dossier = racine, donnee perimee).
            continue
        try:
            data = json.loads(
                (directory / RESUME_JSON).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        pharm = data.get("pharmacology")
        if not isinstance(pharm, dict):
            pharm = {}
        pw = data.get("psychonautwiki_graphql")
        if not isinstance(pw, dict):
            pw = {}
        fields: dict[str, str] = {}
        wiki_values = _wiki_infobox_values(data)
        # TARGET_ORDER reste couvert en entier (le champ « interactions »
        # alimente les interactions locales) ; ses champs non listes plus la
        # liste des colonnes de comparaison ne sont pas affiches.
        for key in dict.fromkeys(list(rm.TARGET_ORDER) + list(rm.COMPARE_ORDER)):
            if key == "name":
                continue
            value = ""
            entry = pharm.get(key)
            if isinstance(entry, dict):
                value = _flatten_value(entry.get("value"))
            elif isinstance(entry, str):
                value = entry.strip()
            if not value:  # repli sur les champs bruts PW
                raw = pw.get(key)
                if raw is None and key == "cross_tolerance":
                    for cand in ("cross_tolerances", "crossTolerance"):
                        if pw.get(cand) is not None:
                            raw = pw.get(cand)
                            break
                value = _flatten_value(raw)
            if not value:  # dernier repli : infobox Wikipedia du resume
                value = _wiki_value(wiki_values, key)
            elif key == "halflife" and not _starts_with_duration(value):
                # phrase de Wikipedia citant le metabolite plutot que la
                # molecule : la ligne d'infobox, plus structuree, fait foi
                value = _wiki_value(wiki_values, key) or value
            if value:
                fields[key] = value
        out.append({
            "name": str(data.get("molecule") or directory.name),
            "path": str(directory),
            "fields": fields,
        })
    return out


def find_molecule_dir(root: Path, name: str) -> Optional[Path]:
    """Dossier molecule correspondant a `nom` (casse et accents ignores).

    Teste pour chaque dossier, dans l'ordre : le nom declare dans
    `resume.json`, celui de `.pharma.json`, enfin le nom du dossier. Un
    dossier qui a deja un `resume.json` est prefere a un simple dossier
    de meme nom. Renvoie None si rien ne correspond.
    """
    root = Path(root) if root is not None else None
    target = rm.fold_accents(str(name or "")).casefold().strip()
    if not target or root is None or not root.is_dir():
        return None
    fallback: Optional[Path] = None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        directory = Path(dirpath)
        if directory == root:
            # la racine n'est jamais une molecule : son resume ou son
            # association ne doivent pas capter une recherche de dossier.
            continue
        candidates: list[str] = []
        if RESUME_JSON in filenames:
            try:
                data = json.loads(
                    (directory / RESUME_JSON).read_text(encoding="utf-8")
                )
                if isinstance(data, dict) and data.get("molecule"):
                    candidates.append(str(data["molecule"]))
            except (OSError, ValueError):
                pass
        declared = read_molecule_meta(directory)
        if declared:
            candidates.append(declared)
        candidates.append(directory.name)
        if any(rm.fold_accents(c).casefold().strip() == target
               for c in candidates if c):
            if RESUME_JSON in filenames:
                return directory
            if fallback is None:
                fallback = directory
    return fallback


# --- tableaux (comparaison + interactions) : enregistrement / chargement -----
TABLES_FORMAT = "pharma-tableaux"
TABLES_VERSION = 1


class TablesError(Exception):
    """Fichier de tableaux absent, illisible ou d'un format inconnu."""


def tables_payload(compare_items, interaction_names, root=None,
                   hidden_columns=None) -> dict:
    """Dictionnaire enregistre pour les deux tableaux.

    `compare_items` : lignes du tableau de comparaison, dans l'ordre affiche
    ({name, dir}). `interaction_names` : molecules etudiees de la page
    Interactions. Le format est versionne pour rester relisible.
    """
    items: list[dict] = []
    for item in compare_items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        if not name:
            continue
        directory = item.get("dir")
        items.append({
            "molecule": name,
            "dossier": str(directory) if directory else None,
        })
    names = [str(n).strip() for n in (interaction_names or [])
             if str(n).strip()]
    return {
        "format": TABLES_FORMAT,
        "version": TABLES_VERSION,
        "racine": str(root) if root else None,
        "comparaison": items,
        "interactions": names,
        "colonnes_masquees": sorted(str(c) for c in (hidden_columns or [])),
    }


def save_tables(path, compare_items, interaction_names, root=None,
                hidden_columns=None, which: str = "both") -> Path:
    """Ecrit les tableaux dans un fichier JSON.

    `which` peut etre `"both"`, `"comparaison"` ou `"interactions"`.
    Cree le dossier parent si besoin. Leve OSError si l'ecriture echoue.
    """
    path = Path(path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    payload = tables_payload(compare_items if which in ("both", "comparaison")
                             else [], interaction_names if which in ("both", "interactions")
                             else [], root, hidden_columns)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_tables(path, which: str = "both") -> dict:
    """Lit un fichier ecrit par `save_tables`.

    `which` filtre la partie restauree (`"both"`, `"comparaison"`, `"interactions"`).

    Renvoie {'comparaison': [{'name', 'dir'}], 'interactions': [nom],
    'colonnes_masquees': [...], 'racine': str|None}. Leve TablesError si le
    fichier est absent, illisible ou d'un format inconnu.
    """
    path = Path(path)
    if not path.is_file():
        raise TablesError(f"Fichier introuvable : {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TablesError(f"Fichier illisible : {exc}") from exc
    if not isinstance(data, dict):
        raise TablesError("Contenu inattendu (objet JSON attendu).")
    fmt = data.get("format")
    if fmt not in (None, TABLES_FORMAT):
        raise TablesError(f"Format inconnu : {fmt!r}.")
    version = data.get("version")
    if isinstance(version, int) and version > TABLES_VERSION:
        raise TablesError(
            f"Version {version} trop récente (maximum {TABLES_VERSION})."
        )
    compare: list[dict] = []
    if which in ("both", "comparaison"):
        for item in data.get("comparaison") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("molecule", item.get("name", "")) or "").strip()
            if not name:
                continue
            directory = item.get("dossier", item.get("dir"))
            compare.append({"name": name,
                            "dir": str(directory) if directory else None})
    names = []
    if which in ("both", "interactions"):
        names = [str(n).strip() for n in (data.get("interactions") or [])
                 if str(n).strip()]
    hidden = [str(c) for c in (data.get("colonnes_masquees") or []) if str(c)]
    return {
        "comparaison": compare,
        "interactions": names,
        "colonnes_masquees": hidden,
        "racine": str(data["racine"]) if data.get("racine") else None,
    }


# --- calendrier des prises ----------------------------------------------------
# Journal personnel : une entree = molecule + date/heure + dose (texte libre).
# L'outil ne propose aucune dose et ne donne aucun conseil : il enregistre ce
# que l'utilisateur saisit et represente la periode d'effet estimee d'apres
# les durees documentees de la molecule (voir molecule_timeline).
CALENDAR_FORMAT = "pharma-calendrier"
CALENDAR_VERSION = 1

# Unites acceptees dans les durees de resume.json -> minutes.
_DURATION_UNITS: tuple[tuple[str, float], ...] = (
    ("secondes", 1.0 / 60.0), ("seconde", 1.0 / 60.0), ("seconds", 1.0 / 60.0),
    ("second", 1.0 / 60.0), ("secs", 1.0 / 60.0), ("sec", 1.0 / 60.0),
    ("minutes", 1.0), ("minute", 1.0), ("mins", 1.0), ("min", 1.0),
    ("heures", 60.0), ("heure", 60.0), ("hours", 60.0), ("hour", 60.0),
    ("hrs", 60.0), ("hr", 60.0),
    ("jours", 1440.0), ("jour", 1440.0), ("days", 1440.0), ("day", 1440.0),
    ("h", 60.0), ("min", 1.0), ("d", 1440.0), ("s", 1.0 / 60.0),
)

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")

# Premier « nombre [a-b] unite » du texte : sert a la demi-vie, dont les
# phrases de Wikipedia commencent souvent par la valeur (« 8 hours and a time
# to peak of about 1 to 2 hours ») puis citent d'autres chiffres.
_LEAD_DURATION_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:[-–—]\s*(\d+(?:[.,]\d+)?))?\s*"
    r"(secondes|seconde|seconds|second|secs|sec|minutes|minute|mins|min|"
    r"heures|heure|hours|hour|hrs|hr|jours|jour|days|day|h|d|s)\b"
)
_UNIT_FACTOR = dict(_DURATION_UNITS)

# Valeurs d'infobox : la duree peut etre separee de son unite par un encadre
# (« 8.7 (range 4.6-16) hours »). On lit le premier nombre et on ignore
# l'encadre, ce que ne fait pas `parse_lead_duration`.
_INFOBOX_DURATION_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:[-–—]\s*(\d+(?:[.,]\d+)?)\s*)?"
    r"(?:\([^)]{0,60}\)\s*)?"
    r"(secondes|seconde|seconds|second|secs|sec|minutes|minute|mins|min|"
    r"heures|heure|hours|hour|hrs|hr|jours|jour|days|day|h|d|s)\b"
)


def _starts_with_duration(text) -> bool:
    """Vrai si le texte commence par « nombre [plage] unite ».

    Distingue une valeur documentee (« 175 minutes », « 1–3 days ») d'une
    phrase de Wikipedia ou l'extraction a glisse le chiffre du metabolite
    (« norfluoxetine is longer (16 days)… »), qui ne doit pas faire foi.
    """
    raw = str(text or "").strip().lower()
    return bool(raw) and _LEAD_DURATION_RE.match(raw) is not None


def parse_infobox_duration(text) -> Optional[tuple[float, float]]:
    """Premiere duree d'une valeur d'infobox, en minutes ; None si illisible.

    Plus tolerant que `parse_lead_duration` : un encadre peut separer la
    valeur de son unite (« 8.7 (range 4.6-16) hours »).
    """
    raw = str(text or "").strip().lower()
    if not raw:
        return None
    match = _INFOBOX_DURATION_RE.search(raw)
    if match is None:
        return None
    unit = _UNIT_FACTOR.get(match.group(3))
    if unit is None:
        return None
    low = float(match.group(1).replace(",", ".")) * unit
    raw_high = match.group(2)
    high = float(raw_high.replace(",", ".")) * unit if raw_high else low
    return (min(low, high), max(low, high))


def calendar_path() -> Path:
    """Journal des prises global (~/.local/share/pharma/calendrier.json).

    Chemin de repli, utilisé seulement hors répertoire de travail ; quand un
    répertoire est ouvert, le journal vit dans celui-ci (`calendrier.json`).
    """
    base = os.environ.get("XDG_DATA_HOME") or ""
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "pharma" / "calendrier.json"


def parse_duration_minutes(text) -> Optional[tuple[float, float]]:
    """« 3.0-5.0 hours » -> (180.0, 300.0) minutes ; None si illisible.

    Accepte une plage (« a-b ») comme une valeur simple, en secondes,
    minutes, heures ou jours (anglais ou francais). Seul le premier
    groupe separe par « ; » est lu (un resume peut lister une duree par
    voie d'administration).
    """
    raw = str(text or "").strip().lower()
    if not raw:
        return None
    raw = raw.split(";")[0]
    unit = None
    for word, factor in _DURATION_UNITS:
        if re.search(r"(?<![a-z])" + word + r"(?![a-z])", raw):
            unit = factor
            break
    if unit is None:
        return None
    numbers = [float(n.replace(",", ".")) for n in _NUMBER_RE.findall(raw)]
    if not numbers:
        return None
    return (min(numbers) * unit, max(numbers) * unit)


def parse_lead_duration(text) -> Optional[tuple[float, float]]:
    """Premiere duree citee dans un texte, en minutes ; None si illisible.

    Contrairement a `parse_duration_minutes`, seuls la valeur et l'unite qui
    se suivent sont lues : les phrases de Wikipedia (« 8 hours and a time to
    peak of about 1 to 2 hours ») citent d'autres chiffres apres la valeur.
    Utilise pour la demi-vie.
    """
    raw = str(text or "").strip().lower()
    if not raw:
        return None
    match = _LEAD_DURATION_RE.search(raw)
    if match is None:
        return None
    unit = _UNIT_FACTOR.get(match.group(3))
    if unit is None:
        return None
    low = float(match.group(1).replace(",", ".")) * unit
    raw_high = match.group(2)
    high = float(raw_high.replace(",", ".")) * unit if raw_high else low
    return (min(low, high), max(low, high))


def _pharmacology_text(pharm: dict, key: str) -> str:
    entry = pharm.get(key)
    if isinstance(entry, dict):
        for field_name in ("value", "pw", "wiki"):
            text = str(entry.get(field_name) or "").strip()
            if text:
                return text
        return ""
    if isinstance(entry, str):
        return entry.strip()
    return ""


def _load_resume(molecule_dir: Optional[Path]) -> dict:
    """`resume.json` d'un dossier molecule, ou {} s'il est absent/illisible."""
    if not molecule_dir:
        return {}
    try:
        data = json.loads(
            (Path(molecule_dir) / RESUME_JSON).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _halflife_choice(pharm: dict, data: dict
                     ) -> tuple[Optional[tuple[float, float]], str]:
    """(plage en minutes, texte source) de la demi-vie documentee.

    Le champ fusionne `pharmacology.halflife` fait foi quand il *commence* par
    la duree. Sinon — phrase de Wikipedia ou l'extraction a glisse le chiffre
    du metabolite — on retombe sur la ligne « half-life » de l'infobox
    Wikipedia, structuree ; a defaut, la phrase est relue telle quelle.
    """
    text = _pharmacology_text(pharm, "halflife")
    if _starts_with_duration(text):
        return parse_lead_duration(text), text
    wiki = _wiki_value(_wiki_infobox_values(data), "halflife")
    value = parse_infobox_duration(wiki) if wiki else None
    if value:
        return value, wiki
    return parse_lead_duration(text), text


def halflife_document(molecule_dir: Optional[Path]) -> dict:
    """Demi-vie documentee d'une molecule : plage (minutes) et texte source.

    Renvoie {"range": (min, max) | None, "text": texte d'origine}. `text`
    permet d'afficher la valeur telle que documentee, par exemple
    « 1–3 days (acute) 4–6 days (chronic) ».
    """
    data = _load_resume(molecule_dir)
    pharm = data.get("pharmacology")
    if not isinstance(pharm, dict):
        pharm = {}
    value, text = _halflife_choice(pharm, data)
    return {"range": value, "text": text}


def molecule_timeline(molecule_dir: Optional[Path]) -> dict:
    """Durees d'action documentees d'une molecule, en minutes.

    Lit `pharmacology` du `resume.json` et renvoie les phases de la plus
    fiable a la moins directe : « onset » (temps d'apparition), « comeup »
    (montee), « peak » (plateau), « offset » (descente), « total » (duree
    d'action du resume si presente, sinon somme des phases lues) et
    « halflife » (demi-vie documentee). Chaque valeur est une plage (min, max)
    en minutes, ou None. Sert a estimer la periode sous influence dans le
    calendrier ; ce n'est pas une donnee medicale.
    """
    timeline: dict[str, Optional[tuple[float, float]]] = {
        "onset": None, "comeup": None, "peak": None, "offset": None,
        "total": None, "halflife": None,
    }
    if not molecule_dir:
        return timeline
    data = _load_resume(molecule_dir)
    if not data:
        return timeline
    pharm = data.get("pharmacology")
    if not isinstance(pharm, dict):
        pharm = {}
    for key in ("onset", "comeup", "peak", "offset"):
        timeline[key] = parse_duration_minutes(_pharmacology_text(pharm, key))
    timeline["total"] = parse_duration_minutes(
        _pharmacology_text(pharm, "duration")
    )
    # la demi-vie fusionnee est parfois une phrase citant le metabolite : on
    # prefere alors la valeur structuree de l'infobox Wikipedia (voir
    # `_halflife_choice`)
    timeline["halflife"] = _halflife_choice(pharm, data)[0]
    if timeline["total"] is None:
        parts = [timeline[k] for k in ("onset", "comeup", "peak", "offset")
                 if timeline[k]]
        if parts:
            timeline["total"] = (sum(p[0] for p in parts),
                                 sum(p[1] for p in parts))
    return timeline


def halflife_points(start: datetime, timeline: Optional[dict] = None,
                    limit: int = 8) -> list[tuple[int, datetime]]:
    """Multiples de la demi-vie documentee apres une prise.

    Renvoie [(1, t1), (2, t2), ...] ou tk = prise + k x demi-vie (borne basse
    de la plage documentee), jusqu'a `limit`. Liste vide si la demi-vie n'est
    pas connue.
    """
    if not isinstance(start, datetime):
        return []
    half = (timeline or {}).get("halflife")
    if not half or float(half[0]) <= 0:
        return []
    step = timedelta(minutes=float(half[0]))
    return [(k, start + step * k) for k in range(1, int(limit) + 1)]


def influence_segments(timeline: Optional[dict] = None,
                       override_min: Optional[float] = None
                       ) -> list[tuple[str, float, float]]:
    """Phases d'une prise, en minutes apres la prise.

    Renvoie [(libelle, debut, fin), ...] parmi « latence », « montee »,
    « plateau », « descente » ; liste vide si aucune duree n'est connue.
    Les valeurs hautes des plages documentees sont utilisees pour couvrir
    toute la periode possible.

    `override_min` (duree saisie a la main par l'utilisateur) remplace
    l'estimation.
    """
    if override_min:
        return [("durée indiquée", 0.0, float(override_min))]
    timeline = timeline or {}
    onset = timeline.get("onset") or (0.0, 0.0)
    segments: list[tuple[str, float, float]] = []
    if onset[0] > 0:
        segments.append(("latence", 0.0, float(onset[0])))
    cursor = float(onset[0])
    comeup = timeline.get("comeup")
    mont_end = float(onset[1]) + (float(comeup[1]) if comeup else 0.0)
    if mont_end > cursor:
        segments.append(("montée", cursor, mont_end))
    cursor = max(cursor, mont_end)
    peak = timeline.get("peak")
    if peak:
        end = cursor + float(peak[1])
        segments.append(("plateau", cursor, end))
        cursor = end
    offset = timeline.get("offset")
    if offset:
        end = cursor + float(offset[1])
        segments.append(("descente", cursor, end))
        cursor = end
    if not segments:
        total = timeline.get("total")
        if total:
            segments.append(("durée documentée", 0.0, float(total[1])))
    return segments


def influence_end_minutes(timeline: Optional[dict] = None,
                          override_min: Optional[float] = None) -> float:
    """Fin de la periode d'effet, en minutes apres la prise (0 si inconnue)."""
    segments = influence_segments(timeline, override_min)
    return max((end for _, _, end in segments), default=0.0)


# --- pharmacocinetique : courbe d'absorption / elimination -------------------
#
# Modele simple a un compartiment : absorption du premier ordre (constante
# `ka`) puis elimination du premier ordre (constante `ke = ln 2 / demi-vie`).
# La concentration relative suit la fonction de Bateman
#
#   c(t) = (exp(-ke t) - exp(-ka t)) / (exp(-ke Tmax) - exp(-ka Tmax))
#
# normalisee a 1 au sommet (Tmax), puis mise a l'echelle par la dose et la
# biodisponibilite quand elles sont chiffrees. Le volume de distribution
# n'etant pas documente, l'axe vertical reste relatif (unites arbitraires) :
# c'est une estimation documentaire, jamais une posologie.

# duree representee par defaut : 5 demi-vies (~97 % elimine)
PK_HALF_LIVES = 5

# unites de dose converties en milligrammes (texte libre de l'utilisateur)
DOSE_UNITS = {
    "µg": 0.001, "μg": 0.001, "ug": 0.001, "mcg": 0.001, "ng": 1e-06,
    "mg": 1.0, "g": 1000.0,
}


def _midpoint(value) -> float:
    """Milieu d'une plage (min, max) en minutes ; 0 si absente ou illisible."""
    try:
        return (float(value[0]) + float(value[1])) / 2.0
    except (TypeError, ValueError, IndexError):
        return 0.0


def parse_dose_mg(text) -> Optional[float]:
    """Dose texte (« 150 µg », « 1,5 g », « 100 mg ») convertie en mg.

    Seul le premier nombre suivi d'une unite est lu ; sans unite, la valeur
    est prise en milligrammes. Renvoie None si aucun nombre n'est lisible.
    """
    raw = str(text or "").strip().lower()
    if not raw:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(µg|μg|ug|mcg|mg|g|ng)?", raw)
    if match is None:
        return None
    value = float(match.group(1).replace(",", "."))
    unit = match.group(2) or "mg"
    return value * DOSE_UNITS.get(unit, 1.0)


def parse_fraction(text) -> Optional[float]:
    """« 71 % » ou « 0,71 » -> 0.71 ; None si illisible ou hors de ]0, 1]."""
    raw = str(text or "").strip().lower()
    if not raw:
        return None
    numbers = _NUMBER_RE.findall(raw.split(";")[0])
    if not numbers:
        return None
    value = float(numbers[0].replace(",", "."))
    if "%" in raw or value > 1.0:
        value /= 100.0
    return value if 0.0 < value <= 1.0 else None


def _solve_ka(ke: float, tmax: float) -> float:
    """Constante d'absorption telle que le sommet du modele tombe sur le Tmax.

    Resout `Tmax = ln(ka/ke) / (ka - ke)` par dichotomie, avec `ka > ke`.
    """
    low = ke * (1.0 + 1e-9)
    high = ke * 1e6
    for _ in range(100):
        middle = math.sqrt(low * high)
        value = math.log(middle / ke) / (middle - ke)
        if value > tmax:
            low = middle
        else:
            high = middle
    return math.sqrt(low * high)


def pk_parameters(timeline: Optional[dict] = None, *,
                  tmax_min: Optional[float] = None,
                  bioavailability: Optional[float] = None) -> dict:
    """Parametres du modele a un compartiment, d'apres les durees documentees.

    `ke = ln 2 / demi-vie` ; `ka` est resolu pour que le sommet tombe sur le
    Tmax (fourni, sinon fin de la montee `onset + come-up`, sinon estime a
    demi-vie / 2). Renvoie un dictionnaire vide si la demi-vie manque.
    """
    timeline = timeline or {}
    half = timeline.get("halflife")
    if not half or float(half[0]) <= 0:
        return {}
    half_min = _midpoint(half)
    ke = math.log(2) / half_min
    if tmax_min and float(tmax_min) > 0:
        tmax = float(tmax_min)
        source = "Tmax fourni"
    else:
        onset = timeline.get("onset")
        comeup = timeline.get("comeup")
        if onset or comeup:
            tmax = _midpoint(onset) + _midpoint(comeup)
            source = "fin de la montee (onset + come-up)"
        else:
            tmax = half_min / 2.0
            source = "estimation (demi-vie / 2)"
    # au-dela de ~1/ke, l'absorption serait plus lente que l'elimination :
    # hors du domaine du modele (flip-flop), on borne le Tmax.
    limit = 0.98 / ke
    if tmax >= limit:
        tmax = limit
        source += " — borne du modele"
    total = timeline.get("total")
    return {
        "half_life_min": half_min,
        "half_life_range": half,
        "ke": ke,
        "ka": _solve_ka(ke, tmax),
        "tmax_min": tmax,
        "tmax_source": source,
        "total_high": float(total[1]) if total else 0.0,
        "bioavailability": bioavailability,
    }


def pk_curve(parameters: Optional[dict], *, dose_mg: Optional[float] = None,
             end_min: Optional[float] = None, points: int = 361
             ) -> tuple[list[float], list[float]]:
    """Courbe d'une prise : (temps en minutes, concentration relative).

    L'echelle verticale est relative (u.a.) : proportionnelle a la dose
    chiffree et a la biodisponibilite quand elles sont connues, sinon
    normalisee a 1 au sommet. Duree par defaut : `PK_HALF_LIVES` demi-vies
    (au moins la duree d'action documentee).
    """
    if not parameters:
        return [], []
    ke = parameters["ke"]
    ka = parameters["ka"]
    tmax = parameters["tmax_min"]
    peak = math.exp(-ke * tmax) - math.exp(-ka * tmax)
    if peak <= 0:
        return [], []
    if not end_min:
        end_min = max(PK_HALF_LIVES * parameters["half_life_min"],
                      4.0 * tmax, float(parameters.get("total_high") or 0.0))
    scale = float(dose_mg) if dose_mg else 1.0
    bio = parameters.get("bioavailability")
    if bio:
        scale *= float(bio)
    points = max(2, int(points))
    times = [end_min * index / (points - 1) for index in range(points)]
    values = [scale * (math.exp(-ke * t) - math.exp(-ka * t)) / peak
              for t in times]
    return times, values


def _read_bioavailability(molecule_dir: Optional[Path]) -> Optional[float]:
    """Biodisponibilite chiffree du resume d'une molecule (fraction), si lue."""
    if molecule_dir is None:
        return None
    try:
        data = json.loads(
            (Path(molecule_dir) / RESUME_JSON).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pharm = data.get("pharmacology")
    if not isinstance(pharm, dict):
        return None
    return parse_fraction(_pharmacology_text(pharm, "bioavailability"))


def pharmacokinetic_report(molecule_dir: Optional[Path] = None, dose: str = "",
                           *, timeline: Optional[dict] = None,
                           tmax_min: Optional[float] = None) -> dict:
    """Prepare la courbe PK d'une prise : parametres, points et messages.

    `dose` est du texte libre (« 150 µg ») ; seule la valeur chiffree sert a
    l'echelle verticale. Le dictionnaire renvoye est toujours exploitable :
    `ok`, `message` si la courbe est impossible, sinon `times`, `values`,
    `dose_mg`, `parameters`.
    """
    timeline = (timeline if timeline is not None
                else molecule_timeline(molecule_dir))
    params = pk_parameters(
        timeline, tmax_min=tmax_min,
        bioavailability=_read_bioavailability(molecule_dir),
    )
    dose_mg = parse_dose_mg(dose)
    if not params:
        return {
            "ok": False,
            "message": "Demi-vie non documentée : la courbe "
                       "pharmacocinétique ne peut pas être estimée pour "
                       "cette molécule (renseignez son résumé, ou vérifiez "
                       "que le dossier est dans le répertoire de travail).",
            "parameters": {}, "times": [], "values": [], "dose_mg": dose_mg,
        }
    times, values = pk_curve(params, dose_mg=dose_mg)
    document = halflife_document(molecule_dir)
    return {"ok": True, "message": "", "times": times, "values": values,
            "dose_mg": dose_mg, "parameters": params,
            "half_life_text": document.get("text") or ""}


def normalise_entry(entry: dict) -> dict:
    """Entree de calendrier propre : molecule, start ISO, dose, note, duree."""
    entry = entry if isinstance(entry, dict) else {}
    duree = entry.get("duree_min")
    try:
        duree = int(float(duree)) if duree not in (None, "", 0) else None
    except (TypeError, ValueError):
        duree = None
    return {
        "molecule": str(entry.get("molecule") or "").strip(),
        "start": str(entry.get("start") or "").strip(),
        "dose": str(entry.get("dose") or "").strip(),
        "note": str(entry.get("note") or "").strip(),
        "duree_min": duree,
    }


def save_calendar(entries, path: Optional[Path] = None) -> Path:
    """Ecrit le journal des prises (JSON versionne) et renvoie le chemin."""
    target = Path(path) if path else calendar_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    prises = [normalise_entry(e) for e in (entries or [])]
    prises = [e for e in prises if e["molecule"] and e["start"]]
    prises.sort(key=lambda e: e["start"])
    payload = {
        "format": CALENDAR_FORMAT,
        "version": CALENDAR_VERSION,
        "prises": prises,
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def load_calendar(path: Optional[Path] = None) -> list[dict]:
    """Lit le journal des prises ; liste vide si absent ou illisible."""
    source = Path(path) if path else calendar_path()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    prises = data.get("prises")
    if not isinstance(prises, list):
        return []
    out = [normalise_entry(e) for e in prises if isinstance(e, dict)]
    out = [e for e in out if e["molecule"] and e["start"]]
    out.sort(key=lambda e: e["start"])
    return out


class CalendarError(Exception):
    """Fichier de calendrier absent, illisible ou d'un format inconnu."""


def save_calendar_file(path, entries) -> Path:
    """Ecrit une copie du journal dans un fichier choisi.

    Meme format versionne que le journal par defaut (`save_calendar`) ; le
    dossier parent est cree si besoin. Leve OSError si l'ecriture echoue.
    """
    return save_calendar(entries, Path(path))


def load_calendar_file(path) -> list[dict]:
    """Lit un journal depuis un fichier choisi.

    Accepte le format `pharma-calendrier` (dictionnaire versionne) comme une
    simple liste d'entrees. Les entrees sont nettoyees et triees par date.
    Leve CalendarError si le fichier est absent, illisible ou d'un format
    inconnu.
    """
    path = Path(path)
    if not path.is_file():
        raise CalendarError(f"Fichier introuvable : {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CalendarError(f"Fichier illisible : {exc}") from exc
    if isinstance(data, dict):
        fmt = data.get("format")
        if fmt not in (None, CALENDAR_FORMAT):
            raise CalendarError(f"Format inconnu : {fmt!r}.")
        version = data.get("version")
        if isinstance(version, int) and version > CALENDAR_VERSION:
            raise CalendarError(
                f"Version {version} trop récente "
                f"(maximum {CALENDAR_VERSION})."
            )
        raw = data.get("prises")
    elif isinstance(data, list):
        raw = data
    else:
        raise CalendarError("Contenu inattendu (journal JSON attendu).")
    if not isinstance(raw, list):
        raise CalendarError("Contenu inattendu (liste de prises attendue).")
    out = [normalise_entry(e) for e in raw if isinstance(e, dict)]
    out = [e for e in out if e["molecule"] and e["start"]]
    out.sort(key=lambda e: e["start"])
    return out


# --- resume (parser) ---------------------------------------------------------
def resume_markdown_path(molecule_dir: Path) -> Path:
    return Path(molecule_dir) / RESUME_MD


def resume_json_path(molecule_dir: Path) -> Path:
    return Path(molecule_dir) / RESUME_JSON


def resume_html_path(molecule_dir: Path) -> Path:
    return Path(molecule_dir) / RESUME_HTML


def resume_molecule_name(molecule_dir: Path) -> str:
    """Nom de molecule declare dans `resume.json` ('' si absent/illisible).

    Permet a l'interface d'afficher le nom **reel** du resume plutot que le
    nom du dossier, qui peut avoir ete renomme ou mal associe.
    """
    try:
        data = json.loads(
            read_text_or_empty(resume_json_path(molecule_dir))
        )
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("molecule") or "").strip()


def read_text_or_empty(path: Path) -> str:
    """Contenu d'un fichier texte, ou chaine vide s'il est absent/illisible."""
    path = Path(path)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def read_resume_markdown(molecule_dir: Path) -> str:
    """Contenu de l'ancien `resume.md`, ou chaine vide (compatibilite)."""
    return read_text_or_empty(resume_markdown_path(molecule_dir))


def ensure_resume_html(molecule_dir: Path) -> str:
    """HTML du resume pret a afficher, **sans aucun acces reseau**.

    - si `resume.html` existe deja, son contenu est renvoye tel quel ;
    2. sinon, si `resume.json` est present, le HTML est reconstruit
       localement (et le fichier ecrit) a partir du rapport enregistre ;
    3. sinon, chaine vide : a l'appelant de proposer une regeneration.
    """
    molecule_dir = Path(molecule_dir)
    html = read_text_or_empty(resume_html_path(molecule_dir))
    if html:
        return html
    payload = read_text_or_empty(resume_json_path(molecule_dir))
    if not payload:
        return ""
    try:
        data = json.loads(payload)
    except ValueError:
        return ""
    try:
        html = report_to_html(rm.MoleculeReport.from_dict(data))
    except Exception:  # pragma: no cover - fichier ancien ou corrompu
        return ""
    if not html:
        return ""
    try:
        resume_html_path(molecule_dir).write_text(html, encoding="utf-8")
    except OSError:
        pass
    return html


def has_resume(molecule_dir: Path) -> bool:
    """Vrai si le dossier contient un resume affichable (html, md ou json)."""
    molecule_dir = Path(molecule_dir)
    for candidate in (
        resume_html_path(molecule_dir),
        resume_markdown_path(molecule_dir),
        resume_json_path(molecule_dir),
    ):
        if read_text_or_empty(candidate):
            return True
    return False


def write_resume(report: rm.MoleculeReport, molecule_dir: Path,
                 progress: Progress = None) -> Path:
    """Ecrit `resume.json` et `resume.html` dans le dossier molecule.

    Un seul fichier HTML est ecrit (le rapport complet) : l'ancien
    `resume.md` est supprime pour ne pas occuper deux fois la place.
    Le rapport doit avoir ete construit par rm.build_report().
    """
    molecule_dir = Path(molecule_dir)
    molecule_dir.mkdir(parents=True, exist_ok=True)
    rm.save_json(report, str(resume_json_path(molecule_dir)), quiet=True)
    resume_html_path(molecule_dir).write_text(
        report_to_html(report), encoding="utf-8"
    )
    resume_markdown_path(molecule_dir).unlink(missing_ok=True)
    if progress is not None:
        progress(f"Resume enregistre dans {molecule_dir}")
    return resume_html_path(molecule_dir)


def build_report(molecule: str,
                 choices: Optional[dict[str, str]] = None,
                 progress: Progress = None) -> rm.MoleculeReport:
    """Delegue a recherche_molecule.build_report (point d'entree unique)."""
    return rm.build_report(molecule, choices, progress=progress)


def refresh_resume(molecule_dir: Path, molecule: str,
                   choices: Optional[dict[str, str]] = None,
                   progress: Progress = None) -> rm.MoleculeReport:
    """Regenere resume.json / resume.html d'un dossier de molecule existant.

    Ne touche pas au PDF : sert au bouton « Regenerer le resume ».
    """
    report = build_report(molecule, choices, progress=progress)
    write_resume(report, Path(molecule_dir), progress=progress)
    return report


# --- Wikipedia : titre et PDF ------------------------------------------------
def wikipedia_pdf_url(title: str) -> str:
    """URL du PDF genere par Wikipedia pour un titre de page."""
    slug = quote(title.replace(" ", "_"), safe="")
    return f"{REST_API}/page/pdf/{slug}"


def wikipedia_page_url(title: str) -> str:
    return f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='')}"


def psychonautwiki_client() -> rm.WikiClient:
    """Client MediaWiki de PsychonautWiki."""
    return rm.WikiClient(
        rm.PW_MEDIAWIKI, "https://psychonautwiki.org", "PsychonautWiki"
    )


def resolve_psychonautwiki_title(query: str, choice: str = "",
                                 progress: Progress = None) -> str:
    """Titre exact de la page PsychonautWiki correspondant a `query`.

    Leve rm.AmbiguousResults si le resultat est ambigu et qu'aucun choix n'est
    fourni : l'appelant demande alors a l'utilisateur dans l'interface.
    """
    query = (query or "").strip()
    if not query:
        raise AcquireError("Nom de page PsychonautWiki vide.")
    if choice:
        return choice
    if progress is not None:
        progress(f"Recherche PsychonautWiki : « {query} » ...")
    client = psychonautwiki_client()
    try:
        results = client.search(query)
    except RuntimeError as exc:
        raise AcquireError(f"Recherche PsychonautWiki impossible : {exc}") from exc
    titles = [str(r.get("title", "")) for r in results if r.get("title")]
    if not titles:
        raise AcquireError(f"Aucune page PsychonautWiki trouvee pour '{query}'.")
    return rm.resolve_title(titles, query, "PsychonautWiki", "pw")


def psychonautwiki_html(title: str, progress: Progress = None) -> str:
    """HTML rendu d'une page PsychonautWiki (titre deja choisi).

    L'interface transforme ce HTML en PDF ; echec leve AcquireError.
    """
    title = (title or "").strip()
    if not title:
        raise AcquireError("Titre PsychonautWiki vide.")
    if progress is not None:
        progress(f"Recuperation de la page PsychonautWiki « {title} » ...")
    client = psychonautwiki_client()
    try:
        html = client.parse_html(title)
    except RuntimeError as exc:
        raise AcquireError(f"Page PsychonautWiki illisible : {exc}") from exc
    if not html or not html.strip():
        raise AcquireError(f"Page PsychonautWiki vide : '{title}'.")
    return html


def psychonautwiki_pdf_name(title: str) -> str:
    """Nom de fichier PDF d'une page PsychonautWiki dans un dossier molecule."""
    return f"{PW_PDF_PREFIX}{slugify(title, fallback='page')}.pdf"


def resolve_wikipedia_title(query: str, choice: str = "") -> str:
    """Titre exact de la page Wikipedia correspondant a `query`.

    Leve rm.AmbiguousResults si plusieurs pages correspondent et qu'aucun
    choix n'est fourni : l'appelant demande alors a l'utilisateur.
    """
    query = (query or "").strip()
    if not query:
        raise AcquireError("Titre Wikipedia vide.")
    if choice:
        return choice
    client = rm.WikiClient(
        rm.EN_WIKI_MEDIAWIKI, "https://en.wikipedia.org", "Wikipedia (anglais)"
    )
    try:
        results = client.search(query)
    except RuntimeError as exc:
        raise AcquireError(f"Recherche Wikipedia impossible : {exc}") from exc
    titles = [str(r.get("title", "")) for r in results if r.get("title")]
    if not titles:
        raise AcquireError(f"Aucune page Wikipedia trouvee pour '{query}'.")
    return rm.resolve_title(titles, query, "Wikipedia (anglais)", "wp")


def is_pdf(path: Path) -> bool:
    """Vrai si le fichier commence par la signature PDF."""
    try:
        with open(path, "rb") as handle:
            return handle.read(5).startswith(b"%PDF")
    except OSError:
        return False


def download_wikipedia_pdf(title: str, dest_dir: Path,
                           filename: str = "",
                           fraction: Fraction = None) -> Path:
    """Telecharge le PDF d'une page Wikipedia dans `dest_dir`.

    Retourne le chemin ecrit. Le fichier est valide (signature PDF verifiee)
    avant d'etre place : un echec laisse le dossier propre.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = filename or f"{PDF_PREFIX}{slugify(title)}.pdf"
    target = unique_path(dest_dir / name)
    tmp = target.with_name(target.name + ".part")

    url = wikipedia_pdf_url(title)
    try:
        with requests.get(
            url, stream=True, timeout=HTTP_TIMEOUT,
            headers={"User-Agent": rm.USER_AGENT},
        ) as resp:
            if resp.status_code != 200:
                raise AcquireError(
                    f"PDF Wikipedia indisponible (HTTP {resp.status_code}) "
                    f"pour '{title}'."
                )
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as handle:
                for chunk in resp.iter_content(1 << 16):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    if fraction is not None and total:
                        fraction(min(done / total, 1.0))
    except requests.RequestException as exc:
        tmp.unlink(missing_ok=True)
        raise AcquireError(f"Telechargement Wikipedia echoue : {exc}") from exc

    if not is_pdf(tmp):
        tmp.unlink(missing_ok=True)
        raise AcquireError(
            f"Le fichier recu pour '{title}' n'est pas un PDF."
        )
    tmp.replace(target)
    return target


# --- actions du repertoire de travail ----------------------------------------
def create_subdirectory(root: Path, parent: Path, name: str) -> Path:
    """Cree un sous-dossier dans `parent` (verifie sous la racine)."""
    root = Path(root).resolve()
    parent = Path(parent).resolve()
    if not parent.is_dir() or not is_inside(root, parent):
        raise WorkspaceError("Dossier parent invalide.")
    clean = slugify(name)
    if not clean:
        raise WorkspaceError("Nom de dossier invalide.")
    target = unique_path(parent / clean)
    if not is_inside(root, target):
        raise WorkspaceError("Chemin refuse (hors du repertoire de travail).")
    target.mkdir(parents=True, exist_ok=False)
    return target


def import_pdf(root: Path, dest_dir: Path, source: Path,
               filename: str = "") -> Path:
    """Copie un PDF existant dans `dest_dir` (verifie sous la racine)."""
    source = Path(source)
    if not source.is_file():
        raise AcquireError(f"Fichier introuvable : {source}")
    if source.suffix.lower() != ".pdf" or not is_pdf(source):
        raise AcquireError(f"Ce n'est pas un PDF : {source.name}")

    dest_dir = Path(dest_dir)
    if not is_inside(root, dest_dir):
        raise WorkspaceError("Destination refusee (hors du repertoire de travail).")
    dest_dir.mkdir(parents=True, exist_ok=True)

    name = filename or source.name
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    target = unique_path(dest_dir / slugify(name, fallback="document.pdf"))
    if target.suffix.lower() != ".pdf":
        target = target.with_suffix(".pdf")
    shutil.copy2(source, target)
    return target


def import_document(root: Path, dest_dir: Path, source: Path,
                    filename: str = "") -> Path:
    """Copie un document quelconque (PDF, image, texte, EPUB...) dans
    `dest_dir`, en refusant toute evasion hors du repertoire de travail."""
    source = Path(source)
    if not source.is_file():
        raise AcquireError(f"Fichier introuvable : {source}")

    dest_dir = Path(dest_dir)
    if not is_inside(root, dest_dir):
        raise WorkspaceError("Destination refusee (hors du repertoire de travail).")
    dest_dir.mkdir(parents=True, exist_ok=True)

    name = filename or source.name
    target = unique_path(dest_dir / slugify(name, fallback="document"))
    if not is_inside(root, target):
        raise WorkspaceError("Chemin refuse (hors du repertoire de travail).")
    shutil.copy2(source, target)
    return target


def add_molecule(root: Path, parent: Path, molecule: str,
                 choices: Optional[dict[str, str]] = None,
                 progress: Progress = None,
                 fraction: Fraction = None) -> dict:
    """Cree `<parent>/<Molecule>/`, y met le PDF Wikipedia et le resume.

    Le dossier ne contient que l'essentiel : le PDF Wikipedia, `resume.json`
    et `resume.html`. Le HTML de la page PsychonautWiki est renvoye (et non
    ecrit) : l'interface en fabrique un PDF si elle le souhaite.

    Retourne {'dir', 'pdf', 'pw_title', 'pw_html', 'resume_json',
    'resume_html', 'title', 'report'}.
    Peut lever rm.AmbiguousResults (l'appelant demande alors le choix).
    """
    molecule = (molecule or "").strip()
    if not molecule:
        raise AcquireError("Nom de molecule vide.")

    molecule_dir = Path(parent) / slugify(molecule, fallback="molecule")
    if not is_inside(root, molecule_dir):
        raise WorkspaceError("Dossier de molecule refuse (hors racine).")
    molecule_dir.mkdir(parents=True, exist_ok=True)

    report = build_report(molecule, choices, progress=progress)
    write_resume(report, molecule_dir, progress=progress)

    pdf_path: Optional[Path] = None
    title = report.wikipedia.title or ""
    try:
        if not title:
            title = resolve_wikipedia_title(molecule, (choices or {}).get("wp", ""))
        if progress is not None:
            progress(f"Telechargement du PDF Wikipedia '{title}' ...")
        pdf_path = download_wikipedia_pdf(title, molecule_dir, fraction=fraction)
    except rm.AmbiguousResults:
        raise
    except (AcquireError, WorkspaceError) as exc:
        # le resume reste exploitable meme sans PDF
        if progress is not None:
            progress(f"PDF Wikipedia non recupere : {exc}")

    return {
        "dir": molecule_dir,
        "pdf": pdf_path,
        "pw_title": report.pw_page.title or "",
        "pw_html": report.pw_page.html or "",
        "resume_json": resume_json_path(molecule_dir),
        "resume_html": resume_html_path(molecule_dir),
        "title": title,
        "report": report,
    }


# --- rendu HTML du resume (volet droit) --------------------------------------
def escape_html(text: str) -> str:
    """Echappe le texte pour un fragment HTML."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# alias historique interne
_esc = escape_html


def rows_to_html(rows: list[dict], title: str = "",
                 subtitle: str = "") -> str:
    """Tableau HTML (sous-ensemble riche supporte par Qt) pour le volet droit."""
    parts: list[str] = []
    if title:
        parts.append(f"<h2 style='margin:0 0 2px 0'>{_esc(title)}</h2>")
    if subtitle:
        parts.append(
            f"<p style='color:#666;margin:0 0 10px 0'>{_esc(subtitle)}</p>"
        )
    parts.append(
        "<table width='100%' border='1' cellspacing='0' cellpadding='6' "
        "bordercolor='#dcdcdc'>"
        "<tr bgcolor='#f2f5f9'>"
        "<th align='left' width='32%'>Champ</th>"
        "<th align='left'>Valeur</th>"
        "</tr>"
    )
    for row in rows:
        label = _esc(row.get("label", ""))
        value_html = ""
        row_parts = row.get("parts")
        if row_parts:
            chunks = [
                f"<b>{_esc(sub_label)} :</b> {_esc(sub_value)}"
                for sub_label, sub_value in row_parts
            ]
            value_html = "<br/>".join(chunks)
        else:
            value_html = _esc(row.get("value", ""))
        parts.append(
            f"<tr><td valign='top'><b>{label}</b></td>"
            f"<td valign='top'>{value_html}</td></tr>"
        )
    parts.append("</table>")
    return "<html><body>" + "".join(parts) + "</body></html>"


def report_to_html(report: rm.MoleculeReport) -> str:
    """Tableau HTML complet pour un rapport deja construit."""
    rows = rm.report_rows(report)
    subtitle_parts = []
    for source_name, page in (
        ("PsychonautWiki", report.pw_page),
        ("Wikipedia (anglais)", report.wikipedia),
    ):
        if getattr(page, "found", False):
            subtitle_parts.append(f"{source_name} : {page.title}")
    if not rows:
        erreurs = " | ".join(
            e for e in (report.pw_substance.error, report.wikipedia.error) if e
        )
        subtitle_parts.append(erreurs or "aucune donnee exploitable")
    return rows_to_html(rows, title=report.molecule,
                        subtitle=" · ".join(subtitle_parts))


# --- recherche de livres et d'articles (sources libres) ----------------------
@dataclass
class BookHit:
    """Resultat de recherche bibliographique (livre ou article)."""
    source: str = ""
    title: str = ""
    authors: str = ""
    year: str = ""
    url: str = ""
    download_url: str = ""
    note: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class ArticleHit:
    """Resultat de recherche PubMed / Europe PMC : metadonnees + PDF."""
    source: str = ""
    pmid: str = ""
    pmcid: str = ""
    doi: str = ""
    title: str = ""
    authors: str = ""
    journal: str = ""
    year: str = ""
    url: str = ""
    pdf_urls: list[str] = field(default_factory=list)
    note: str = ""
    extra: dict = field(default_factory=dict)


def _get_json(url: str, params: Optional[dict] = None) -> dict:
    try:
        resp = requests.get(
            url, params=params, timeout=HTTP_TIMEOUT,
            headers={"User-Agent": rm.USER_AGENT},
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        raise AcquireError(f"Requete impossible ({url}) : {exc}") from exc
    except ValueError as exc:
        raise AcquireError(f"Reponse illisible ({url}) : {exc}") from exc


def _get_xml(url: str, params: Optional[dict] = None):
    """GET + parse XML. Leve AcquireError en cas d'echec."""
    try:
        resp = requests.get(
            url, params=params, timeout=HTTP_TIMEOUT,
            headers={"User-Agent": rm.USER_AGENT},
        )
        resp.raise_for_status()
        return ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise AcquireError(f"Reponse XML illisible ({url}) : {exc}") from exc
    except requests.RequestException as exc:
        raise AcquireError(f"Requete impossible ({url}) : {exc}") from exc


def search_open_library(query: str, limit: int = 20) -> list[BookHit]:
    """Recherche de livres via l'API Open Library (metadonnees + pret)."""
    data = _get_json(OL_SEARCH, {
        "q": query, "limit": limit,
        "fields": "title,author_name,first_publish_year,key,ia,ebook_access",
    })
    hits: list[BookHit] = []
    for doc in data.get("docs", []):
        key = str(doc.get("key", "") or "")
        ia_ids = [str(i) for i in (doc.get("ia") or [])]
        access = str(doc.get("ebook_access", "") or "")
        note = {
            "public": "texte integral disponible",
            "borrowable": "emprunt numerique possible",
            "printdisabled": "accessible en lecture adaptee uniquement",
            "no_ebook": "pas de version numerique libre",
        }.get(access, access or "acces en ligne")
        hits.append(BookHit(
            source="Open Library",
            title=str(doc.get("title", "") or ""),
            authors=", ".join(str(a) for a in (doc.get("author_name") or [])[:3]),
            year=str(doc.get("first_publish_year", "") or ""),
            url=f"https://openlibrary.org{key}" if key else "",
            download_url=(
                f"https://archive.org/details/{ia_ids[0]}" if ia_ids else ""
            ),
            note=note,
            extra={"openlibrary_key": key, "ia": ia_ids},
        ))
    return hits


def _ia_is_downloadable(doc: dict) -> bool:
    """Vrai si Internet Archive signale l'element comme libre de droits."""
    rights = str(doc.get("rights", "") or "").lower()
    licenseurl = str(doc.get("licenseurl", "") or "").lower()
    if "public domain" in rights or "publicdomain" in licenseurl:
        return True
    collections = doc.get("collection") or []
    if isinstance(collections, str):
        collections = [collections]
    return any(
        c in ("gutenberg", "librivoxaudio", "opensource", "publicdomain")
        for c in collections
    )


def search_internet_archive(query: str, limit: int = 20) -> list[BookHit]:
    """Recherche sur Internet Archive ; telechargement si domaine public."""
    params = {
        "q": f"{query} AND mediatype:texts",
        "rows": limit,
        "page": 1,
        "output": "json",
        "fl[]": [
            "identifier", "title", "creator", "year",
            "rights", "licenseurl", "collection",
        ],
    }
    data = _get_json(IA_SEARCH, params)
    response = data.get("response", {})
    hits: list[BookHit] = []
    for doc in response.get("docs", []):
        identifier = str(doc.get("identifier", "") or "")
        creators = doc.get("creator") or []
        if isinstance(creators, str):
            creators = [creators]
        free = _ia_is_downloadable(doc)
        hits.append(BookHit(
            source="Internet Archive",
            title=str(doc.get("title", "") or ""),
            authors=", ".join(str(c) for c in creators[:3]),
            year=str(doc.get("year", "") or ""),
            url=f"https://archive.org/details/{identifier}" if identifier else "",
            download_url=(
                f"https://archive.org/download/{identifier}/{identifier}.pdf"
                if identifier and free else ""
            ),
            note="domaine public : telechargement direct" if free
                 else "consultation / emprunt en ligne",
            extra={"identifier": identifier, "public_domain": free},
        ))
    return hits


def search_pubmed_central(query: str, limit: int = 20) -> list[BookHit]:
    """Recherche d'articles en acces libre sur PubMed Central (PMC)."""
    ids_data = _get_json(f"{PMC_EUTILS}/esearch.fcgi", {
        "db": "pmc", "term": query, "retmax": limit, "retmode": "json",
    })
    ids = ids_data.get("esearchresult", {}).get("idlist", []) or []
    if not ids:
        return []
    summary = _get_json(f"{PMC_EUTILS}/esummary.fcgi", {
        "db": "pmc", "id": ",".join(ids), "retmode": "json",
    })
    result = summary.get("result", {})
    hits: list[BookHit] = []
    for pmc_id in ids:
        doc = result.get(str(pmc_id)) or {}
        if not doc:
            continue
        authors = ", ".join(
            str(a.get("name", "")) for a in (doc.get("authors") or [])[:3]
        )
        article_url = (
            f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{pmc_id}/"
        )
        hits.append(BookHit(
            source="PubMed Central",
            title=str(doc.get("title", "") or ""),
            authors=authors,
            year=str(doc.get("pubdate", "") or "")[:4],
            url=article_url,
            # acces libre par definition, mais le serveur peut filtrer les
            # robots : l'utilisateur a toujours le lien de secours.
            download_url=f"{article_url}pdf/",
            note="acces libre (PMC)",
            extra={"pmc_id": pmc_id},
        ))
    return hits


def search_literature(query: str, limit: int = 20) -> list[BookHit]:
    """Interroge les trois sources et concatene les resultats."""
    hits: list[BookHit] = []
    errors: list[str] = []
    for func in (search_pubmed_central, search_internet_archive,
                 search_open_library):
        try:
            hits.extend(func(query, limit=limit))
        except AcquireError as exc:
            errors.append(f"{func.__name__} : {exc}")
    if not hits and errors:
        raise AcquireError(" | ".join(errors))
    return hits


# --- PubMed (articles scientifiques) -----------------------------------------
PUBMED_SORTS = {
    "pertinence": "relevance",
    "plus recent": "pub date",
    "plus ancien": "pub date",
}


def _pubmed_articleids(doc: dict) -> dict:
    """Extrait doi / pmcid / pii / pmc depuis esummary.articleids."""
    out: dict = {"pmid": str(doc.get("uid", "") or "")}
    for entry in (doc.get("articleids") or []):
        key = str(entry.get("idtype", "") or "").lower()
        value = str(entry.get("value", "") or "")
        if not key or not value:
            continue
        if key == "doi":
            out["doi"] = value
        elif key == "pmc":
            out["pmcid"] = value if value.startswith("PMC") else f"PMC{value}"
        elif key == "pii":
            out["pii"] = value
    return out


def search_pubmed(query: str, limit: int = 20,
                  progress: Progress = None) -> list[ArticleHit]:
    """Recherche PubMed via l'API E-utilities officielle du NCBI.

    Retourne des ArticleHit avec identifiants (pmid/doi), journal, annee et
    la liste des PDF libres connus (Europe PMC). Le choix et le telechargement
    restent a la charge de l'utilisateur (voir download_article).
    """
    query = (query or "").strip()
    if not query:
        raise AcquireError("Requete PubMed vide.")

    if progress is not None:
        progress(f"Recherche PubMed : « {query} » ...")
    search_data = _get_json(f"{PUBMED_EUTILS}/esearch.fcgi", {
        "db": "pubmed",
        "term": query,
        "retmax": str(int(limit)),
        "retmode": "json",
        "sort": "relevance",
    })
    ids = search_data.get("esearchresult", {}).get("idlist", []) or []
    if not ids:
        return []

    if progress is not None:
        progress(f"Details de {len(ids)} article(s) ...")
    summary = _get_json(f"{PUBMED_EUTILS}/esummary.fcgi", {
        "db": "pubmed",
        "id": ",".join(ids),
        "retmode": "json",
    })
    result = summary.get("result", {})

    # Enrichissement : Europe PMC fournit le PMCID et les PDF en acces libre.
    pdf_by_pmid: dict[str, list[str]] = {}
    try:
        pdf_by_pmid = _epmc_pdf_urls_by_pmid(ids)
    except AcquireError:
        # best effort : la recherche reste utilisable sans les PDF libres
        pdf_by_pmid = {}

    hits: list[ArticleHit] = []
    for pmid in ids:
        doc = result.get(str(pmid)) or {}
        if not doc:
            continue
        ids_map = _pubmed_articleids(doc)
        authors = doc.get("authors") or []
        authors_str = ", ".join(
            str(a.get("name", "")) for a in authors[:6]
        )
        if len(authors) > 6:
            authors_str += " et al."
        journal = str(doc.get("fulljournalname") or doc.get("source") or "")
        year = str(doc.get("pubdate") or "")[:4]
        pmcid = ids_map.get("pmcid", "")
        pdfs = list(pdf_by_pmid.get(pmid, []))
        hits.append(ArticleHit(
            source="PubMed",
            pmid=pmid,
            pmcid=pmcid,
            doi=ids_map.get("doi", ""),
            title=str(doc.get("title") or ""),
            authors=authors_str,
            journal=journal,
            year=year,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            pdf_urls=pdfs,
            note=journal or "PubMed",
            extra={"articleids": ids_map, "sort_query": query},
        ))
    if progress is not None:
        progress(f"{len(hits)} article(s) PubMed.")
    return hits


def _epmc_pdf_urls_by_pmid(pmids: list[str]) -> dict[str, list[str]]:
    """PDF libres (Europe PMC) indexes par PMID.

    Interroge l'API Europe PMC en une seule requete. Leve AcquireError si
    l'API est indisponible (appelant peut ignorer).
    """
    if not pmids:
        return {}
    or_terms = " OR ".join(f"EXT_ID:{p}" for p in pmids)
    data = _get_json(f"{EPMC_REST}/search", {
        "query": f"({or_terms}) AND (SRC:MED)",
        "format": "json",
        "pageSize": str(len(pmids)),
        "resultType": "core",
    })
    out: dict[str, list[str]] = {}
    for doc in (data.get("resultList", {}) or {}).get("result", []) or []:
        pmid = str(doc.get("pmid") or "")
        if not pmid:
            continue
        urls: list[str] = []
        for entry in (doc.get("fullTextUrlList", {}) or {}).get("fullTextUrl", []) or []:
            url = str(entry.get("url") or "")
            if not url:
                continue
            style = str(entry.get("documentStyle") or "").lower()
            # uniquement les liens qui pointent reellement vers un PDF
            if style != "pdf" and "pdf=" not in url.lower():
                continue
            if url not in urls:
                urls.append(url)
        out[pmid] = urls
    return out


def _semantic_scholar_pdf(doi: str) -> str:
    """URL de PDF libre donnee par Semantic Scholar (API ouverte, sans cle).

    L'API anonyme limite fortement le debit : on patiente une fois apres un
    429 avant d'abandonner. Renvoie une chaine vide en cas d'echec — c'est
    un candidat parmi d'autres, jamais une dependence.
    """
    url = f"{SEMANTIC_SCHOLAR_API}/DOI:{quote(doi, safe='/')}"
    headers = {"User-Agent": rm.USER_AGENT, "Accept": "application/json"}
    for attempt in range(2):
        try:
            resp = requests.get(url, params={"fields": "openAccessPdf"},
                                timeout=HTTP_TIMEOUT, headers=headers)
        except requests.RequestException:
            return ""
        if resp.status_code == 429 and attempt == 0:
            time.sleep(2.0)
            continue
        if resp.status_code != 200:
            return ""
        try:
            payload = resp.json()
        except ValueError:
            return ""
        return str((payload.get("openAccessPdf") or {}).get("url") or "")
    return ""


def article_pdf_candidates(hit: ArticleHit,
                           progress: Progress = None,
                           email: str = "") -> list[str]:
    """Liste des URL de PDF possibles, du plus fiable au moins fiable.

    Ordre : PDF signales par Europe PMC, puis PDF trouves par OpenAlex,
    Semantic Scholar et Unpaywall, et enfin les liens « ?pdf=render »
    d'Europe PMC qui renvoient souvent 403 hors navigateur (on essaie quand
    meme, en derniere position). `email` active Unpaywall et sert de contact
    poli aux autres API (defaut : variable UNPAYWALL_EMAIL).
    """
    email = (email or "").strip() or UNPAYWALL_EMAIL
    direct: list[str] = []
    deferred: list[str] = []
    for url in (hit.pdf_urls or []):
        if not url or url in direct or url in deferred:
            continue
        if "europepmc.org" in url and "pdf=" in url:
            deferred.append(url)
        else:
            direct.append(url)

    candidates: list[str] = list(direct)
    doi = hit.doi or ""

    # OpenAlex : index d'ouverture qui pointe vers les PDF editeurs et
    # les repositories, sans condition de contact.
    if doi or hit.pmid:
        if progress is not None:
            progress(f"Recherche de PDF libre (OpenAlex) pour {doi or hit.pmid} ...")
        data: dict = {}
        if doi:
            try:
                data = _get_json(
                    f"{OPENALEX_API}/works/doi:{quote(doi, safe='')}",
                    {"mailto": email} if email else None,
                )
            except AcquireError:
                data = {}
        if not data and hit.pmid:
            try:
                params: dict = {"filter": f"ids.pmid:{hit.pmid}", "per-page": 1}
                if email:
                    params["mailto"] = email
                page = _get_json(f"{OPENALEX_API}/works", params)
                results = list(page.get("results") or [])
                data = results[0] if results else {}
            except AcquireError:
                data = {}
        found: list[str] = []
        open_access = data.get("open_access") or {}
        if isinstance(open_access, dict) and open_access.get("oa_url"):
            found.append(str(open_access["oa_url"]))
        for location in (data.get("locations") or []):
            if location.get("pdf_url"):
                found.append(str(location["pdf_url"]))
        for url in found:
            if url and url not in candidates and url not in deferred:
                candidates.append(url)

    # Semantic Scholar : autre index d'acces libre (« openAccessPdf »),
    # API ouverte sans cle mais agressive sur les limites de debit.
    if doi:
        if progress is not None:
            progress(f"Recherche de PDF libre (Semantic Scholar) pour {doi} ...")
        scholar_url = _semantic_scholar_pdf(doi)
        if scholar_url and scholar_url not in candidates \
                and scholar_url not in deferred:
            candidates.append(scholar_url)

    # Unpaywall : API gratuite mais exige une adresse de contact.
    if doi and email:
        if progress is not None:
            progress(f"Recherche de PDF libre (Unpaywall) pour {doi} ...")
        try:
            data = _get_json(f"{UNPAYWALL_API}/{quote(doi, safe='')}", {
                "email": email,
            })
        except AcquireError:
            data = {}
        for location in (data.get("oa_locations") or []):
            pdf_url = str(location.get("url_for_pdf") or "")
            if pdf_url and pdf_url not in candidates and pdf_url not in deferred:
                candidates.append(pdf_url)

    candidates.extend(deferred)

    if progress is not None:
        progress(
            f"{len(candidates)} PDF(s) libre(s) connu(s)."
            if candidates else "Aucun PDF libre connu pour cet article."
        )
    return candidates


def download_article(root: Optional[Path], hit: ArticleHit, dest_dir: Path,
                     filename: str = "",
                     progress: Progress = None,
                     fraction: Fraction = None,
                     fallback: Optional[Callable[[], Optional[Path]]] = None,
                     email: str = "") -> Path:
    """Telecharge le PDF d'un article PubMed dans `dest_dir`.

    `root` (racine du repertoire de travail) est verifie si fourni.
    Essaye chaque URL candidate jusqu'a obtenir un fichier PDF valide.
    `fallback` est appele quand aucun PDF direct ne fonctionne : dans
    l'interface, il genere un PDF a partir du texte integral Europe PMC
    (voir fulltext_html). Leve AcquireError si tout echoue.
    """
    dest_dir = Path(dest_dir).resolve()
    if root is not None:
        root = Path(root).resolve()
        if not is_inside(root, dest_dir):
            raise WorkspaceError("Destination refusee (hors du repertoire de travail).")
    if not dest_dir.is_dir() and not dest_dir.parent.is_dir():
        raise WorkspaceError(f"Destination introuvable : {dest_dir}")

    candidates = article_pdf_candidates(hit, progress=progress, email=email)
    errors: list[str] = []
    for url in candidates:
        if progress is not None:
            progress(f"Telechargement : {url} ...")
        try:
            return download_file(
                url, dest_dir, filename=filename, fraction=fraction,
                root=root, referer=hit.url,
            )
        except AcquireError as exc:
            errors.append(str(exc))
            continue

    # Repli : texte integral JATS -> PDF genere localement (toujours legal,
    # Europe PMC ne publie que des articles en acces libre).
    if fallback is not None:
        if progress is not None:
            progress(
                "PDF directs refusés ou indisponibles : "
                "texte integral Europe PMC ..."
            )
        try:
            path = fallback()
        except AcquireError as exc:
            errors.append(str(exc))
        else:
            if path:
                return path

    if errors:
        if not candidates:
            # une seule cause connue : le repli, son message est le plus precis
            raise AcquireError(errors[0])
        raise AcquireError("Telechargement impossible : " + " | ".join(errors))
    raise AcquireError(
        "Aucun PDF libre n'est connu pour cet article. "
        "Ouvrez la page PubMed pour y acceder via votre institution."
    )


def download_file(url: str, dest_dir: Path, filename: str = "",
                  fraction: Fraction = None,
                  root: Optional[Path] = None,
                  referer: str = "") -> Path:
    """Telecharge une URL vers `dest_dir` (nom deduit si non fourni).

    Si `root` est fourni, la destination doit se trouver dessous.
    Un fichier dont le nom finit par .pdf doit contenir un vrai PDF.
    Les entetes imitent un navigateur classique (Accept, langue) et portent
    `referer` quand on le connait : certains editeurs les exigent.
    """
    dest_dir = Path(dest_dir)
    if root is not None and not is_inside(Path(root), dest_dir.resolve()):
        raise WorkspaceError("Destination refusee (hors du repertoire de travail).")
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = slugify(filename) if filename else ""
    if not name:
        name = slugify(Path(url.split("?")[0]).name) or "document.pdf"
    if not Path(name).suffix:
        name += ".pdf"
    target = unique_path(dest_dir / name)
    tmp = target.with_name(target.name + ".part")
    headers = {
        "User-Agent": rm.USER_AGENT,
        "Accept": "application/pdf,application/xhtml+xml,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    try:
        with requests.get(
            url, stream=True, timeout=HTTP_TIMEOUT, allow_redirects=True,
            headers=headers,
        ) as resp:
            if resp.status_code != 200:
                raise AcquireError(
                    f"Telechargement refuse (HTTP {resp.status_code}) : {url}"
                )
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as handle:
                for chunk in resp.iter_content(1 << 16):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    if fraction is not None and total:
                        fraction(min(done / total, 1.0))
    except requests.RequestException as exc:
        tmp.unlink(missing_ok=True)
        raise AcquireError(f"Telechargement echoue : {exc}") from exc

    if target.suffix.lower() == ".pdf" and not is_pdf(tmp):
        tmp.unlink(missing_ok=True)
        raise AcquireError(
            "La reponse n'est pas un PDF (lien direct probablement protege) : "
            "utilisez « Ouvrir la page » pour y acceder depuis le navigateur."
        )
    tmp.replace(target)
    return target


# --- texte integral Europe PMC (repli de telechargement) --------------------
# Quand les PDF d'editeurs refusent les robots (403 tres frequents), l'API
# Europe PMC fournit quand meme le texte integral JATS des articles presents
# dans PMC, en acces libre : on le convertit en HTML puis en PDF localement.

_JATS_DROP = frozenset({"graphic", "inline-graphic", "media",
                        "supplementary-material", "label", "alt-text",
                        "long-desc"})
_JATS_TAGS = {
    "p": "p", "list-item": "li", "italic": "i", "em": "em", "bold": "b",
    "sup": "sup", "sub": "sub", "underline": "u", "monospace": "code",
    "preformat": "pre", "sc": "span", "ref": "li", "fn": "li",
    "table": "table", "thead": "thead", "tbody": "tbody", "tr": "tr",
    "th": "th", "td": "td", "article-title": "b", "statement": "p",
    "title": "h2", "break": "br",
}


def _jats_name(tag) -> str:
    """Nom de balise JATS sans namespace ni prefixe."""
    return str(tag).rsplit("}", 1)[-1].split(":")[-1]


def _jats_first(root, name: str):
    """Premier element de ce nom dans l'arbre (insensible au namespace)."""
    for node in root.iter():
        if _jats_name(node.tag) == name:
            return node
    return None


def _parse_jats(raw: bytes):
    """Parse le XML JATS d'Europe PMC, memes prefixes non declares."""
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        text = raw.decode("utf-8", "replace")
        text = re.sub(r'\sxmlns:[\w.\-]+="[^"]*"', "", text)
        text = re.sub(r"\s([\w.\-]+):([\w.\-]+)=", r" \2=", text)
        text = re.sub(r"</?([\w.\-]+):",
                      lambda m: m.group(0).replace(f"{m.group(1)}:", "", 1),
                      text)
        try:
            return ET.fromstring(text)
        except ET.ParseError as exc:
            raise AcquireError(f"Texte integral illisible : {exc}") from exc


def _jats_inner(el) -> str:
    """Contenu interne d'un element JATS : texte + enfants rendus."""
    return esc_html(el.text or "") + "".join(_jats_render(c) for c in el)


def _jats_render(el, level: int = 0) -> str:
    """Element JATS -> equivalent HTML (queue de balise comprise).

    `level` ne sert qu'aux titres de sections : la section de plus haut
    niveau devient un h2 (le titre de l'article est deja un h1 d'en-tete).
    """
    name = _jats_name(el.tag)
    tail = esc_html(el.tail or "")

    if name in _JATS_DROP:
        return tail

    if name == "sec":
        heading = f"h{min(level + 2, 5)}"
        parts: list[str] = []
        for child in el:
            child_name = _jats_name(child.tag)
            if child_name == "title":
                parts.append(f"<{heading}>{_jats_inner(child)}</{heading}>")
            elif child_name == "sec":
                parts.append(_jats_render(child, level + 1))
            else:
                parts.append(_jats_render(child, level))
        return (f"<section>{esc_html(el.text or '')}{''.join(parts)}"
                f"</section>{tail}")

    if name == "abstract":
        return f'<div class="abstract">{_jats_inner(el)}</div>{tail}'

    if name in ("fig", "table-wrap"):
        label = ""
        caption = None
        for child in el:
            child_name = _jats_name(child.tag)
            if child_name == "label":
                label = "".join(child.itertext()).strip()
            elif child_name == "caption":
                caption = child
        out = ['<div class="figure-block">']
        if label:
            out.append(f'<div class="fig-label">{esc_html(label)}</div>')
        if caption is not None:
            # div et non p : les legendes JATS contiennent leurs propres <p>
            out.append(f'<div class="fig-caption">{_jats_inner(caption)}</div>')
        has_graphic = any(_jats_name(n.tag) in ("graphic", "inline-graphic")
                          for n in el.iter())
        if name == "fig" and has_graphic:
            out.append('<div class="fig-caption">Image non exportée '
                       '(voir la page de l\'article).</div>')
        for child in el:
            if _jats_name(child.tag) in ("label", "caption", "graphic",
                                         "alt-text", "long-desc"):
                continue
            out.append(_jats_render(child, level))
        out.append("</div>")
        return "".join(out) + tail

    if name == "list":
        kind = str(el.get("list-type") or "").lower()
        tag = "ol" if kind in ("ordered", "number", "alpha", "roman") else "ul"
        return (f"<{tag}>{esc_html(el.text or '')}"
                f"{''.join(_jats_render(c, level) for c in el)}</{tag}>{tail}")

    if name == "ref-list":
        heading = "Références"
        parts = []
        for child in el:
            if _jats_name(child.tag) == "title":
                text = "".join(child.itertext()).strip()
                if text:
                    heading = text
                continue
            parts.append(_jats_render(child, level))
        return (f'<h2>{esc_html(heading)}</h2>'
                f'<ol class="reflist">{esc_html(el.text or "")}'
                f"{''.join(parts)}</ol>{tail}")

    if name == "fn-group":
        return (f'<ol class="reflist">{esc_html(el.text or "")}'
                f"{''.join(_jats_render(c, level) for c in el)}</ol>{tail}")

    if name == "kwd-group":
        keywords = ["".join(k.itertext()).strip() for k in el
                    if _jats_name(k.tag) == "kwd"]
        keywords = [k for k in keywords if k]
        if not keywords:
            return tail
        return (f'<p class="keywords"><b>Mots-clés :</b> '
                f'{esc_html(", ".join(keywords))}</p>{tail}')

    if name in ("license-p", "copyright-statement"):
        return f'<p class="licence">{_jats_inner(el)}</p>{tail}'

    if name in ("disp-formula", "inline-formula", "tex-math"):
        return f'<span class="formula">{_jats_inner(el)}</span>{tail}'

    tag = _JATS_TAGS.get(name)
    if tag is None:
        # element non repris explicitement : son texte reste, sans balise
        return _jats_inner(el) + tail
    return f"<{tag}>{_jats_inner(el)}</{tag}>{tail}"


def _resolve_pmcid(hit: ArticleHit, progress: Progress = None) -> str:
    """Identifiant PMC de l'article (requete Europe PMC si absent)."""
    pmcid = str(hit.pmcid or "").strip().upper()
    if pmcid and not pmcid.startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    if pmcid:
        return pmcid
    if hit.doi:
        query = f'DOI:"{hit.doi}"'
    elif hit.pmid:
        query = f"EXT_ID:{hit.pmid} AND SRC:MED"
    else:
        raise AcquireError(
            "Aucun identifiant (PMID/DOI/PMCID) pour retrouver le texte "
            "integral."
        )
    if progress is not None:
        progress("Recherche du texte integral (Europe PMC) ...")
    data = _get_json(f"{EPMC_REST}/search", {
        "query": query, "format": "json", "resultType": "core",
        "pageSize": "1",
    })
    results = list((data.get("resultList") or {}).get("result") or [])
    found = str(results[0].get("pmcid") or "") if results else ""
    found = found.strip().upper()
    if found and not found.startswith("PMC"):
        found = f"PMC{found}"
    if not found:
        raise AcquireError(
            "Pas de texte integral en accès libre (Europe PMC) pour cet "
            "article : ouvrez la page pour y accéder via votre institution."
        )
    return found


def fulltext_html(hit: ArticleHit, progress: Progress = None) -> str:
    """HTML pret a imprimer du texte integral d'un article (Europe PMC).

    Repli de download_article lorsque les PDF d'editeurs refusent les
    robots : Europe PMC diffuse le texte integral JATS des articles de PMC
    en acces libre. Leve AcquireError s'il n'y a pas de texte integral.
    """
    pmcid = _resolve_pmcid(hit, progress)
    if progress is not None:
        progress(f"Telechargement du texte integral {pmcid} ...")
    url = f"{EPMC_REST}/{pmcid}/fullTextXML"
    try:
        resp = requests.get(
            url, timeout=HTTP_TIMEOUT,
            headers={"User-Agent": rm.USER_AGENT,
                     "Accept": "application/xml,text/xml,*/*"},
        )
    except requests.RequestException as exc:
        raise AcquireError(f"Texte integral inaccessible : {exc}") from exc
    if resp.status_code == 404:
        raise AcquireError(
            "Pas de texte integral en accès libre (Europe PMC) pour cet "
            "article : ouvrez la page pour y accéder via votre institution."
        )
    if resp.status_code != 200:
        raise AcquireError(
            f"Texte integral refuse (HTTP {resp.status_code}) pour {pmcid}."
        )
    root = _parse_jats(resp.content)

    title = (hit.title or "").strip()
    if not title:
        node = _jats_first(root, "article-title")
        if node is not None:
            title = "".join(node.itertext()).strip()
    meta = " · ".join(
        text for text in ((hit.authors or "").strip(),
                          (hit.journal or "").strip(),
                          (hit.year or "").strip()) if text
    )

    parts: list[str] = []
    if meta:
        parts.append(f'<p class="doc-authors">{esc_html(meta)}</p>')
    abstract = _jats_first(root, "abstract")
    if abstract is not None:
        parts.append(_jats_render(abstract, 0))
    keywords = _jats_first(root, "kwd-group")
    if keywords is not None:
        parts.append(_jats_render(keywords, 0))
    body = _jats_first(root, "body")
    if body is None:
        raise AcquireError(f"Texte integral vide pour {pmcid}.")
    parts.append(_jats_render(body, 0))
    ref_list = _jats_first(root, "ref-list")
    if ref_list is not None:
        parts.append(_jats_render(ref_list, 0))

    return html_for_print(
        "\n".join(parts),
        title=title or f"Article {pmcid}",
        source_label="Europe PMC — texte integral en accès libre",
        source_url=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
    )


# --- HTML pret a imprimer (rendu PDF via Qt) ---------------------------------
# QTextDocument n'accede jamais au reseau et ignore `display:none` : tout ce
# bloc prépare le HTML (nettoyage, images integrees, feuille de style) pour
# que le rendu PDF soit propre et complet. Les proprietes ci-dessous ont ete
# verifiees une a une contre Qt (selecteurs de classe, fonds, bordures).

PRINT_CSS = """\
body {
  font-family: "DejaVu Sans", "Liberation Sans", Arial, sans-serif;
  font-size: 9.5pt;
  color: #1b1b1b;
}
p { margin: 3pt 0; }
a { color: #14487a; text-decoration: none; }
h1 { font-size: 19pt; color: #12304f; margin: 0 0 6pt 0; }
h2 { font-size: 13pt; color: #12304f; margin: 14pt 0 4pt 0;
     border-bottom: 1px solid #9db4d0; padding-bottom: 2pt; }
h3 { font-size: 11pt; color: #1d3b63; margin: 10pt 0 3pt 0; }
h4, h5 { font-size: 10pt; color: #33415c; margin: 8pt 0 2pt 0; }
ul, ol { margin: 3pt 0 3pt 16pt; }
li { margin: 1.5pt 0; }
table { border-collapse: collapse; margin: 6pt 0; }
th, td { border: 1px solid #b3c0d3; padding: 3pt 5pt; vertical-align: top;
         font-size: 9pt; }
th { background-color: #e8eef7; font-weight: bold; }
blockquote { background-color: #f6f7f9; border-left: 3px solid #b3c0d3;
             padding: 4pt 6pt; margin: 6pt 0; }
img { margin: 3pt 0; }
sup { font-size: 7pt; color: #14487a; }
code, pre { font-family: "DejaVu Sans Mono", monospace; }

/* en-tete du document exporte */
.doc-head { border-bottom: 2px solid #2c5f9e; margin-bottom: 10pt;
            padding-bottom: 5pt; }
.doc-kicker { font-size: 8.5pt; color: #5a7ea6; font-weight: bold; margin: 0; }
.doc-title { font-size: 20pt; color: #12304f; margin: 2pt 0 3pt 0; }
.doc-meta { font-size: 8pt; color: #6b6b6b; margin: 0; }
.doc-authors { font-size: 9pt; color: #444444; font-style: italic;
               margin: 2pt 0 0 0; }

/* marqueurs MediaWiki / PsychonautWiki */
.toc { background-color: #f5f7fa; border: 1px solid #ccd5e2;
       padding: 4pt 8pt; margin: 6pt 0; }
.hatnote { font-style: italic; color: #4a4a4a; }
.thumb, .thumbinner, .center { text-align: center; }
.thumbcaption { font-size: 8.5pt; color: #5b5b5b; margin: 2pt 0 6pt 0; }
.ROAHeader, .ROAHeaderRow, .ROACaption, .ROASubHeader, .ROASubHeaderRow,
.SubstanceTitle, .dosechart, .panel-header, .featured-table th {
  background-color: #e8eef7;
}
.SBInteractionDangerous { background-color: #fbe9e7; border: 1px solid #d9534f;
                          padding: 4pt 6pt; margin: 5pt 0; }
.SBInteractionUnsafe { background-color: #fdf3e3; border: 1px solid #f0ad4e;
                       padding: 4pt 6pt; margin: 5pt 0; }
.SBInteractionUncertain { background-color: #fdfbe8; border: 1px solid #e0cf6b;
                          padding: 4pt 6pt; margin: 5pt 0; }
.pw-disclaimer, .pw-safe-use { background-color: #f4f6f9;
                               border: 1px solid #b9c8dd; padding: 4pt 6pt;
                               margin: 6pt 0; }
.reference, .reflist, .references, .mw-cite-backlink {
  font-size: 8.5pt; color: #3a3a3a;
}

/* blocs produits par la conversion JATS (texte integral Europe PMC) */
.abstract { background-color: #f7f9fc; padding: 5pt 8pt; margin: 6pt 0; }
.figure-block { margin: 8pt 0; }
.fig-label { font-size: 9pt; font-weight: bold; color: #12304f; margin: 4pt 0 1pt 0; }
.fig-caption { font-size: 8.5pt; color: #4a4a4a; margin: 1pt 0; }
.formula { font-family: "DejaVu Sans Mono", monospace; color: #1d3b63; }
.licence { font-size: 8pt; color: #5b5b5b; }
.keywords { font-size: 9pt; color: #333333; }
"""

_MOIS_FR = ("janvier", "février", "mars", "avril", "mai", "juin",
            "juillet", "août", "septembre", "octobre", "novembre", "décembre")

_HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none", re.I)
# elements que QTextDocument ignore ou afficherait de travers dans un PDF
_DROP_TAGS = frozenset({"script", "style", "link", "meta", "noscript", "iframe",
                        "form", "input", "button", "select", "textarea", "svg",
                        "object", "embed", "canvas", "video", "audio"})
# liens d'edition, elements d'impression masquee, bascule de la table des matieres
_DROP_CLASS_KEYS = ("mw-editsection", "noprint", "toctoggle")
_IMG_DROP_ATTRS = ("srcset", "sizes", "decoding", "loading", "fetchpriority",
                   "crossorigin", "referrerpolicy", "usemap", "ismap")


def _date_fr() -> str:
    today = date.today()
    return f"{today.day} {_MOIS_FR[today.month - 1]} {today.year}"


def psychonautwiki_page_url(title: str) -> str:
    """URL canonique d'une page PsychonautWiki (lien figure dans le PDF)."""
    slug = quote((title or "").strip().replace(" ", "_"), safe="_")
    return f"https://psychonautwiki.org/wiki/{slug}"


def clean_html_fragment(html: str, base_url: str = "") -> str:
    """Prepare un fragment HTML pour l'impression Qt.

    * supprime les elements masques (`display:none`) : Qt ne connait pas cette
      propriete, le contenu cache serait sinon imprime tel quel ;
    * retire les utiles d'interface (edition MediaWiki, formulaires, scripts,
      styles de la page, SVG en ligne) ;
    * rend absolus les images et les liens a partir de `base_url` ;
    * assainit les styles en ligne (seules les proprietes utiles restent).
    """
    if not html or not str(html).strip():
        return ""
    if BeautifulSoup is None:  # pragma: no cover - bs4 est dans requirements
        return html
    soup = BeautifulSoup(html, "html.parser")

    def unwanted(tag) -> bool:
        if tag.name in _DROP_TAGS:
            return True
        return any(
            isinstance(cls, str)
            and any(key in cls for key in _DROP_CLASS_KEYS)
            for cls in (tag.get("class") or [])
        )

    def hidden(tag) -> bool:
        return bool(_HIDDEN_STYLE_RE.search(str(tag.get("style") or ""))) \
            or tag.has_attr("hidden")

    # collecte puis decompose, des enfants vers les parents
    doomed = [t for t in soup.find_all(True) if unwanted(t) or hidden(t)]
    for tag in reversed(doomed):
        try:
            tag.decompose()
        except Exception:  # pragma: no cover - jamais observe
            pass

    for tag in soup.find_all(True):
        style = tag.get("style")
        if style:
            kept = [d for d in str(style).split(";")
                    if d.split(":", 1)[0].strip().lower() != "display"]
            if "".join(kept).strip():
                tag["style"] = ";".join(kept)
            else:
                del tag["style"]

    for img in soup.find_all("img"):
        src = str(img.get("src") or "")
        if src and not src.startswith(("data:", "http://", "https://")):
            img["src"] = urljoin(base_url, src)
        for attr in _IMG_DROP_ATTRS:
            if attr in img.attrs:
                del img[attr]

    for anchor in soup.find_all("a"):
        href = str(anchor.get("href") or "")
        if href.startswith("javascript:"):
            anchor["href"] = ""
        elif href and not href.startswith(("http://", "https://", "mailto:", "#")):
            anchor["href"] = urljoin(base_url, href)

    return str(soup)


def _image_mime(data: bytes) -> str:
    """Type MIME d'un octet d'image, ou chaine vide si inconnu."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for signature, mime in ((b"\x89PNG\r\n\x1a\n", "image/png"),
                            (b"\xff\xd8\xff", "image/jpeg"),
                            (b"GIF87a", "image/gif"),
                            (b"GIF89a", "image/gif")):
        if data.startswith(signature):
            return mime
    head = data[:2048].lower()
    if b"<svg" in head and (head.lstrip().startswith(b"<?xml")
                            or head.lstrip().startswith(b"<svg")):
        return "image/svg+xml"
    return ""


def _fetch_image(url: str, max_bytes: int = 6_000_000) -> Optional[bytes]:
    """Telecharge une image (plafond strict) ou renvoie None."""
    try:
        with requests.get(
            url, stream=True, timeout=HTTP_TIMEOUT,
            headers={"User-Agent": rm.USER_AGENT,
                     "Accept": "image/*,*/*;q=0.8"},
        ) as resp:
            if resp.status_code != 200:
                return None
            chunks: list[bytes] = []
            size = 0
            for chunk in resp.iter_content(1 << 16):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except requests.RequestException:
        return None


def embed_remote_images(html: str, base_url: str = "", progress: Progress = None,
                        max_images: int = 40,
                        max_bytes: int = 6_000_000) -> str:
    """Integre les images distantes du HTML sous forme de data: URI.

    QTextDocument ne telecharge rien : sans cette conversion les figures
    apparaissent en blanc dans le PDF. Chaque URL n'est demandee qu'une fois
    (MediaWiki repete ses vignettes) et les echecs sont simplement ignores.
    """
    if BeautifulSoup is None or not html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    images = soup.find_all("img")
    if not images:
        return html
    cache: dict[str, str] = {}
    embedded = 0
    total = 0
    for img in images:
        src = str(img.get("src") or "")
        if not src or src.startswith("data:"):
            continue
        if src not in cache:
            if embedded >= max_images or total >= max_bytes:
                cache[src] = ""
                continue
            uri = ""
            data = _fetch_image(src, max_bytes=max_bytes)
            if data:
                mime = _image_mime(data)
                if mime:
                    uri = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
                    embedded += 1
                    total += len(data)
            cache[src] = uri
        uri = cache[src]
        if uri:
            img["src"] = uri
    if progress is not None and images:
        progress(f"{embedded} image(s) integree(s) au document.")
    return str(soup)


def _document_header(title: str, source_label: str, source_url: str) -> str:
    """Bloc d'en-tete : source, titre, lien et date d'export."""
    if not (title or source_label or source_url):
        return ""
    parts = ['<div class="doc-head">']
    if source_label:
        parts.append(f'<p class="doc-kicker">{esc_html(source_label.upper())}</p>')
    if title:
        parts.append(f'<h1 class="doc-title">{esc_html(title)}</h1>')
    meta: list[str] = []
    if source_url:
        meta.append(f'<a href="{esc_html(source_url)}">{esc_html(source_url)}</a>')
    meta.append(f"exporté le {_date_fr()}")
    parts.append(f'<p class="doc-meta">{" — ".join(meta)}</p>')
    parts.append("</div>")
    return "\n".join(parts)


def html_for_print(html: str, *, title: str = "", source_label: str = "",
                   source_url: str = "", base_url: str = "",
                   fetch_images: bool = False,
                   progress: Progress = None) -> str:
    """Habille un fragment HTML pour le rendu PDF (en-tete + feuille de style).

    `fetch_images=True` integre les figures en data: URI (appels reseau : a
    appeler depuis un thread de travail). L'HTML retourne est un document
    complet, directement lisible par QTextDocument.setHtml().
    """
    base = base_url or source_url
    fragment = clean_html_fragment(html, base)
    if fetch_images:
        fragment = embed_remote_images(fragment, base, progress=progress)
    header = _document_header(title, source_label, source_url)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="fr">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{esc_html(title or source_label)}</title>\n"
        f"<style>\n{PRINT_CSS}\n</style>\n</head>\n<body>\n"
        f"{header}\n{fragment}\n</body>\n</html>\n"
    )


# --- documents hors PDF : texte, images, EPUB --------------------------------
TEXT_SUFFIXES = {".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".log",
                 ".py", ".sh", ".tex", ".bib", ".xml", ".yaml", ".yml",
                 ".ini", ".cfg", ".conf", ".sql"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg",
                  ".webp", ".ico", ".tiff"}
HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
EPUB_SUFFIXES = {".epub"}
DOCX_SUFFIXES = {".docx", ".doc", ".rtf", ".odt"}
AUDIO_VIDEO_SUFFIXES = {".mp3", ".wav", ".ogg", ".flac", ".mp4", ".avi",
                        ".mkv", ".mov", ".webm"}


def document_kind(path: Path) -> str:
    """Catégorie d'un document : pdf | text | image | html | epub | other.

    Sert à l'onglet central et au menu contextuel.
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in TEXT_SUFFIXES:
        return "text"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in HTML_SUFFIXES:
        return "html"
    if suffix in EPUB_SUFFIXES:
        return "epub"
    return "other"


def read_text_document(path: Path, encoding: str = "utf-8") -> str:
    """Lit un fichier texte en tolérant les encodages courants (latin-1)."""
    path = Path(path)
    try:
        return path.read_text(encoding=encoding)
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="latin-1")
        except (OSError, UnicodeDecodeError):
            return ""
    except OSError:
        return ""


def _parse_xml_flexible(raw: bytes):
    """Parse XML en tolérant les préfixes de namespace non déclarés.

    Certains EPUB déclarent `xmlns:dc` oublient… ou l'omettent tout
    simplement : on retente après nettoyage des préfixes plutôt que d'abandonner.
    """
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        text = raw.decode("utf-8", "replace")
        text = re.sub(r'\sxmlns:[\w.\-]+="[^"]*"', "", text)
        text = re.sub(
            r"</?([\w.\-]+):",
            lambda m: m.group(0).replace(f"{m.group(1)}:", "", 1),
            text,
        )
        text = re.sub(r"([\w.\-]+:)([\w.\-]+\s*=)", r"\2", text)
        return ET.fromstring(text)


def _epub_opf_path(zf: zipfile.ZipFile) -> str:
    """Chemin du fichier OPF (package) à l'intérieur de l'archive."""
    container = "META-INF/container.xml"
    if container in zf.namelist():
        try:
            root = _parse_xml_flexible(zf.read(container))
            for node in root.iter():
                if str(node.tag).endswith("rootfile"):
                    full_path = node.attrib.get("full-path", "")
                    if full_path:
                        return full_path
        except ET.ParseError:
            pass
    for name in zf.namelist():
        if name.endswith(".opf"):
            return name
    raise AcquireError("Archive EPUB invalide (OPF introuvable).")


def epub_to_html(path: Path) -> str:
    """Transforme un EPUB en un document HTML autonome (images en base64).

    Le HTML produit est adapté au QTextBrowser de l'onglet central.
    Leve AcquireError si le fichier n'est pas un EPUB valide.
    """
    path = Path(path)
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise AcquireError(f"EPUB illisible : {path.name}") from exc

    with zf:
        opf_path = _epub_opf_path(zf)
        opf_dir = str(Path(opf_path).parent).replace("\\", "/")
        if opf_dir == ".":
            opf_dir = ""
        try:
            opf = _parse_xml_flexible(zf.read(opf_path))
        except ET.ParseError as exc:
            raise AcquireError("EPUB : fichier OPF illisible.") from exc

        manifest: dict[str, dict] = {}
        for item in opf.iter():
            if not item.tag.endswith("item"):
                continue
            item_id = item.attrib.get("id", "")
            manifest[item_id] = {
                "href": item.attrib.get("href", ""),
                "media": item.attrib.get("media-type", ""),
            }

        spine_hrefs: list[str] = []
        for itemref in opf.iter():
            if not itemref.tag.endswith("itemref"):
                continue
            ref = itemref.attrib.get("idref", "")
            entry = manifest.get(ref, {})
            href = entry.get("href", "")
            if not href:
                continue
            spine_hrefs.append(href)
        if not spine_hrefs:
            raise AcquireError("EPUB : aucune page dans la colonne vertébrale.")

        title_node = None
        for node in opf.iter():
            if node.tag.endswith("title"):
                title_node = node
                break
        title = (title_node.text or "").strip() if title_node is not None else ""

        chunks: list[str] = []
        for href in spine_hrefs:
            # décoder l'URL et résoudre le chemin relatif à l'OPF
            from urllib.parse import unquote
            rel = unquote(href)
            member = str((Path(opf_dir) / rel).as_posix()) if opf_dir else rel
            if member not in zf.namelist():
                # tentative directe
                if href in zf.namelist():
                    member = href
                else:
                    continue
            try:
                raw = zf.read(member)
            except KeyError:
                continue
            try:
                text = raw.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue

            # transformer les images relatives en data: URI
            def _replace_img(match):
                src = match.group(1)
                if src.startswith(("http://", "https://", "data:", "#")):
                    return match.group(0)
                img_member = str((Path(member).parent / src).as_posix())
                if img_member in zf.namelist():
                    try:
                        img_data = zf.read(img_member)
                    except KeyError:
                        return match.group(0)
                    import base64 as _b64
                    b64 = _b64.b64encode(img_data).decode("ascii")
                    return f'src="data:image;base64,{b64}"'
                return match.group(0)

            text = re.sub(r'src="([^"]+)"', _replace_img, text)
            # ne garder que le corps si possible
            body_match = re.search(r"<body[^>]*>(.*)</body>", text, re.S | re.I)
            content = body_match.group(1) if body_match else text
            chunks.append(f"<div class='epub-chapter'>{content}</div>")

        body = "\n<hr/>\n".join(chunks)
        header = (
            "<html><head><meta charset='utf-8'/><style>"
            "body { font-family: serif; line-height: 1.5; }"
            "img { max-width: 100%; }"
            "h1, h2, h3 { page-break-before: always; }"
            ".epub-chapter:first-child { page-break-before: avoid; }"
            "</style></head><body>"
        )
        title_html = f"<h1>{_esc(title)}</h1>" if title else ""
        return f"{header}{title_html}{body}</body></html>"


def human_size(num_bytes: int) -> str:
    """Taille lisible (o, Ko, Mo, Go)."""
    size = float(num_bytes)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if size < 1024 or unit == "To":
            return f"{size:.0f} {unit}" if unit == "o" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} To"


# --- interactions entre molecules --------------------------------------------
# Trois sources, du plus local au plus distant :
#   1. les interactions PsychonautWiki deja presentes dans les resume.json
#      (aucun acces reseau) ;
#   2. la base DDInter, telechargee une fois puis interrogee hors ligne
#      (licence CC BY-NC-SA 4.0, https://ddinter.scbdd.com/) ;
#   3. les relations « interaction significative » (P769) de Wikidata
#      (licence CC0, requete SPARQL ponctuelle et mise en cache).
# Simple rapprochement documentaire : ni avis medical, ni conduite a tenir.

DDINTER_CODES: tuple[str, ...] = (
    "A", "B", "C", "D", "G", "H", "J", "L", "M", "N", "P", "R", "S", "V",
)
DDINTER_URL = (
    "https://ddinter.scbdd.com/static/media/download/"
    "ddinter_downloads_code_{code}.csv"
)
DDINTER_LICENCE = "DDInter (CC BY-NC-SA 4.0) — https://ddinter.scbdd.com/"

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIDATA_TTL = 30 * 24 * 3600        # duree de validite du cache des paires

# Alias « nom courant -> nom DDInter » (la base est en anglais). Etendable par
# un fichier synonyms.json dans le dossier de cache : {"Caféine": "Caffeine"}.
DDINTER_ALIASES: dict[str, str] = {
    "acetaminophene": "Acetaminophen",
    "acide acetylsalicylique": "Aspirin",
    "adrenaline": "Epinephrine",
    "alprazolam": "Alprazolam",
    "amitriptyline": "Amitriptyline",
    "amphetamine": "Amphetamine",
    "aspirine": "Aspirin",
    "bupropion": "Bupropion",
    "cafeine": "Caffeine",
    "carbamazepine": "Carbamazepine",
    "clonazepam": "Clonazepam",
    "clozapine": "Clozapine",
    "codeine": "Codeine",
    "cyclobenzaprine": "Cyclobenzaprine",
    "diazepam": "Diazepam",
    "duloxetine": "Duloxetine",
    "escitalopram": "Escitalopram",
    "ethanol": "Ethanol",
    "fluoxetine": "Fluoxetine",
    "haloperidol": "Haloperidol",
    "ibuprofene": "Ibuprofen",
    "imipramine": "Imipramine",
    "ketamine": "Ketamine",
    "lamotrigine": "Lamotrigine",
    "levothyroxine": "Levothyroxine",
    "litium": "Lithium",
    "lithium": "Lithium",
    "lorazepam": "Lorazepam",
    "methadone": "Methadone",
    "methylphenidate": "Methylphenidate",
    "mirtazapine": "Mirtazapine",
    "morphine": "Morphine",
    "naloxone": "Naloxone",
    "naltrexone": "Naltrexone",
    "nicotine": "Nicotine",
    "olanzapine": "Olanzapine",
    "omeprazole": "Omeprazole",
    "paracetamol": "Acetaminophen",
    "paroxetine": "Paroxetine",
    "prednisolone": "Prednisolone",
    "prednisone": "Prednisone",
    "quetiapine": "Quetiapine",
    "risperidone": "Risperidone",
    "sertraline": "Sertraline",
    "tramadol": "Tramadol",
    "valproate": "Valproic acid",
    "venlafaxine": "Venlafaxine",
    "zolpidem": "Zolpidem",
}

# Ordre de gravite (utilise pour trier et colorer).
_INTERACTION_LEVELS: tuple[tuple[str, str, int], ...] = (
    ("contra", "Contre-indiqué", 4),
    ("major", "Majeur", 3),
    ("moder", "Modéré", 2),
    ("minor", "Mineur", 1),
    ("mention", "Mentionné", 1),
    ("aucune", "Aucune", 0),
    ("none", "Aucune", 0),
)

_DDINTER_CACHE: dict = {"key": None, "index": None}


def _norm_name(name: str) -> str:
    """Cle de comparaison d'un nom : sans accents, minuscules, espaces uniques."""
    return re.sub(r"\s+", " ", rm.fold_accents(str(name or "")).casefold()).strip()


def _pair_key(a: str, b: str) -> tuple[str, str]:
    """Cle de paire non ordonnee (memes deux noms = meme cle)."""
    ka, kb = _norm_name(a), _norm_name(b)
    return (ka, kb) if ka <= kb else (kb, ka)


def _word_pattern(name: str):
    """Motif « mot entier » d'un nom de molecule (ou None si trop court)."""
    folded = _norm_name(name)
    if len(folded) < 3:
        return None
    escaped = re.escape(folded)
    return re.compile(rf"(?<![0-9a-z]){escaped}(?![0-9a-z])")


def _mentions(text: str, name: str) -> bool:
    """`text` cite-t-il `name` comme mot entier (casse/accents ignores) ?"""
    pattern = _word_pattern(name)
    if pattern is None or not text:
        return False
    return bool(pattern.search(_norm_name(text)))


def _snippet(text: str, name: str) -> str:
    """Fragment de `text` portant la mention de `name` (pour le detail)."""
    for part in re.split(r"[;\n|]+", str(text or "")):
        if _mentions(part, name):
            return part.strip()
    return str(text or "").strip()[:240]


# -- base DDInter --------------------------------------------------------------
def ddinter_cache_dir() -> Path:
    """Dossier de cache de la base DDInter (~/.cache/pharma/ddinter)."""
    base = os.environ.get("XDG_CACHE_HOME") or ""
    root = Path(base) if base else Path.home() / ".cache"
    return root / "pharma" / "ddinter"


def ddinter_file(cache_dir: Path | None, code: str) -> Path:
    return (Path(cache_dir) if cache_dir else ddinter_cache_dir()) / (
        f"ddinter_downloads_code_{code}.csv"
    )


def ddinter_status(cache_dir: Path | None = None) -> dict:
    """Etat de la base locale : fichiers presents, taille et date."""
    cache_dir = Path(cache_dir) if cache_dir else ddinter_cache_dir()
    present: list[str] = []
    total = 0
    newest = 0.0
    for code in DDINTER_CODES:
        path = ddinter_file(cache_dir, code)
        if not path.is_file():
            continue
        present.append(code)
        try:
            stat = path.stat()
        except OSError:
            continue
        total += stat.st_size
        newest = max(newest, stat.st_mtime)
    return {
        "dir": str(cache_dir),
        "present": present,
        "missing": [c for c in DDINTER_CODES if c not in present],
        "count": len(present),
        "expected": len(DDINTER_CODES),
        "bytes": total,
        "mtime": newest,
        "complete": len(present) == len(DDINTER_CODES),
    }


def download_ddinter(cache_dir: Path | None = None, progress: Progress = None,
                     fraction: Fraction = None) -> dict:
    """Telecharge (ou met a jour) les CSV DDInter, puis retourne leur etat.

    Ecriture atomique (`.part` puis renommage) : un telechargement interrompu
    ne laisse jamais un fichier tronque a la place d'un fichier valide.
    """
    cache_dir = Path(cache_dir) if cache_dir else ddinter_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    total = len(DDINTER_CODES)
    for done, code in enumerate(DDINTER_CODES, start=1):
        if progress:
            progress(f"DDInter : fichier {code} ({done}/{total})…")
        url = DDINTER_URL.format(code=code)
        target = ddinter_file(cache_dir, code)
        tmp = target.with_name(target.name + ".part")
        try:
            with requests.get(
                url, timeout=HTTP_TIMEOUT, stream=True,
                headers={"User-Agent": rm.USER_AGENT},
            ) as resp:
                resp.raise_for_status()
                with tmp.open("wb") as handle:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            handle.write(chunk)
            os.replace(tmp, target)
        except (requests.RequestException, OSError) as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise AcquireError(f"DDInter {code} : {exc}") from exc
        if fraction:
            fraction(done / total)
    return ddinter_status(cache_dir)


def _load_ddinter(cache_dir: Path) -> dict:
    pairs: dict[tuple[str, str], str] = {}
    names: dict[str, str] = {}
    for code in DDINTER_CODES:
        path = ddinter_file(cache_dir, code)
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    a = (row.get("Drug_A") or "").strip()
                    b = (row.get("Drug_B") or "").strip()
                    level = (row.get("Level") or "").strip()
                    if not a or not b:
                        continue
                    names.setdefault(_norm_name(a), a)
                    names.setdefault(_norm_name(b), b)
                    if not level:
                        continue
                    key = _pair_key(a, b)
                    if key not in pairs:
                        pairs[key] = level
        except (OSError, UnicodeDecodeError, csv.Error):
            continue
    return {"pairs": pairs, "names": names, "dir": str(cache_dir)}


def ddinter_index(cache_dir: Path | None = None,
                  use_cache: bool = True) -> dict:
    """Charge les CSV locaux : paires normalisees + noms connus.

    Resultat : {"pairs": {(a, b): niveau}, "names": {cle: nom}, "dir": ...}.
    Memoise tant que les fichiers presents et leur date ne changent pas.
    """
    cache_dir = Path(cache_dir) if cache_dir else ddinter_cache_dir()
    status = ddinter_status(cache_dir)
    key = (status["dir"], tuple(status["present"]), int(status["mtime"]))
    if (use_cache and _DDINTER_CACHE["key"] == key
            and _DDINTER_CACHE["index"] is not None):
        return _DDINTER_CACHE["index"]
    index = _load_ddinter(cache_dir)
    _DDINTER_CACHE.update(key=key, index=index)
    return index


def ddinter_pair_level(index: dict, a: str, b: str) -> str:
    """Niveau DDInter d'une paire (les deux sens), ou ""."""
    if not index:
        return ""
    return (index.get("pairs") or {}).get(_pair_key(a, b), "")


def ddinter_resolve(name: str, index: dict | None = None,
                    synonyms: dict | None = None) -> str:
    """Nom DDInter correspondant a `name`, ou "" si la base l'ignore.

    Ordre : nom exact normalise, table d'alias integree, fichier utilisateur
    `synonyms`, puis orthographe fournie telle quelle.
    """
    key = _norm_name(name)
    if not key:
        return ""
    names = (index or {}).get("names") or {}
    if key in names:
        return names[key]
    mapping: dict[str, str] = dict(DDINTER_ALIASES)
    for src, dst in (synonyms or {}).items():
        if str(dst).strip():
            mapping[_norm_name(src)] = str(dst)
    for candidate in (mapping.get(key), name):
        if not candidate:
            continue
        found = names.get(_norm_name(candidate))
        if found:
            return found
    return ""


def resolve_ddinter_names(names, index: dict | None = None,
                          synonyms: dict | None = None) -> dict[str, str]:
    """{nom demande: nom DDInter} — valeur "" si la base ignore le nom."""
    return {str(n): ddinter_resolve(n, index, synonyms) for n in (names or [])}


def synonyms_path(cache_dir: Path | None = None) -> Path:
    return (Path(cache_dir) if cache_dir else ddinter_cache_dir()) / (
        "synonyms.json"
    )


def load_synonyms(cache_dir: Path | None = None) -> dict:
    """Table d'alias utilisateur (nom courant -> nom DDInter), ou {}."""
    try:
        data = json.loads(synonyms_path(cache_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(v).strip()}


# -- interactions PsychonautWiki locales ---------------------------------------
def workspace_interactions(root: Path) -> dict[str, str]:
    """{molecule: texte des interactions} lu dans les resume.json du travail."""
    out: dict[str, str] = {}
    for entry in comparison_rows(root):
        text = str((entry.get("fields") or {}).get("interactions", "") or "")
        if text.strip():
            out[str(entry["name"])] = text
    return out


# -- niveau et fusion ----------------------------------------------------------
def level_rank(level: str) -> int:
    """Gravite numerique d'un niveau (0 = aucune, 4 = contre-indique)."""
    key = _norm_name(level)
    if not key:
        return 0
    for prefix, _label, rank in _INTERACTION_LEVELS:
        if key.startswith(prefix):
            return rank
    return 1


def level_label(level: str) -> str:
    """Libelle francais d'un niveau DDInter (« Major » -> « Majeur »)."""
    key = _norm_name(level)
    if not key:
        return ""
    for prefix, label, _rank in _INTERACTION_LEVELS:
        if key.startswith(prefix):
            return label
    return str(level).strip()


# -- mecanisme d'interaction (classification indicative, non medicale) ---------
# La classe chimique/psychoactive d'une substance (PsychonautWiki) et son nom
# permettent de deduire des traits pharmacologiques, puis de rapprocher deux
# substances selon un mecanisme probable : synergie, potentialisation, addition,
# antagonisme, metabolisme (voies CYP) ou toxicite. C'est un RAPPROCHEMENT
# DOCUMENTAIRE APPROXIMATIF : il ne remplace ni une base d'interactions
# (DDInter), ni l'avis d'un professionnel. Toute paire peut etre fixee a la
# main dans mechanisms.json (meme principe que synonyms.json).

MECHANISM_LABELS: dict[str, str] = {
    "synergy": "Synergie",
    "potentiation": "Potentialisation",
    "additive": "Addition / cumul",
    "antagonism": "Antagonisme",
    "metabolism": "Métabolisme",
    "toxicity": "Toxicité",
    "unknown": "Non précisé",
}

# trait pharmacologique -> motifs cherches (sans accents, minuscules) dans le
# nom de la molecule et dans ses classes PsychonautWiki.
_TRAIT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "opioid": (
        "opioide", "opioid", "opiace", "morphine", "codeine", "fentanyl",
        "methadone", "oxycodone", "hydrocodone", "heroine", "heroin",
        "tramadol", "buprenorphine", "dihydrocodeine", "dilaudid",
    ),
    "opioid_antagonist": (
        "naloxone", "naltrexone", "nalmefene", "antagoniste opioide",
    ),
    "gabaergic": (
        "benzodiazepine", "benzodiazepin", "barbiturate", "barbiturique",
        "gabaergic", "gaba", "zolpidem", "zopiclone", "zaleplon", "diazepam",
        "lorazepam", "clonazepam", "alprazolam", "midazolam", "oxazepam",
        "phenobarbital", "alcohol", "ethanol", "alcool", "ghb",
        "gamma-hydroxybutyrate",
    ),
    "depressant": (
        "depressant", "depresseur", "sedative", "sedatif", "hypnotic",
        "hypnotique", "anxiolytic", "anxiolytique",
        "central nervous system depressant",
    ),
    "antihistamine": (
        "antihistamine", "antihistaminique", "diphenhydramine", "promethazine",
        "hydroxyzine", "doxylamine", "chlorpheniramine",
    ),
    "stimulant": (
        "stimulant", "stimulante", "psychostimulant", "amphetamine",
        "amfetamine", "methamphetamine", "dextroamphetamine", "methylphenidate",
        "methylphenydate", "cocaine", "cathinone", "khat", "modafinil",
        "caffeine", "cafeine", "nicotine",
    ),
    "sympathomimetic": (
        "sympathomimetic", "sympathomimetique", "phenylephrine",
        "pseudoephedrine", "ephedrine", "adrenaline", "epinephrine",
        "noradrenaline", "norepinephrine", "salbutamol", "albuterol",
    ),
    "serotonergic": (
        "ssri", "isrs", "snri", "irsn", "serotonin", "serotoninergique",
        "serotonergic", "serotonine", "serotonin selective", "tryptophan",
        "tryptophane", "tricyclic", "tricyclique", "amitriptyline",
        "imipramine", "clomipramine", "fluoxetine", "paroxetine", "sertraline",
        "citalopram", "escitalopram", "fluvoxamine", "venlafaxine",
        "duloxetine", "tramadol", "linezolid", "sumatriptan", "triptan",
    ),
    "serotonin_releaser": (
        "serotonin releaser", "liberateur de serotonine", "entactogen",
        "entactogene", "empathogen", "empathogene", "mdma", "mda", "4-mmc",
        "mephedrone", "methylone", "pmma",
    ),
    "maoi": (
        "maoi", "imao", "monoamine oxidase", "mao-a", "mao-b", "harmala",
        "harmaline", "harmine", "ayahuasca", "moclobemide", "phenelzine",
        "tranylcypromine", "selegiline", "isocarboxazid",
    ),
    "psychedelic": (
        "psychedelic", "psychedelique", "hallucinogen", "hallucinogene", "lsd",
        "lysergamide", "lysergamid", "psilocybin", "psilocine", "psilocin",
        "mescaline", "dmt", "2c-b", "2c b", "dob", "doi", "doc", "5-meo-dmt",
    ),
    "dissociative": (
        "dissociative", "dissociatif", "ketamine", "ketamine", "pcp",
        "phencyclidine", "dxm", "dextromethorphan", "methoxetamine", "mxe",
        "nmda",
    ),
    "antipsychotic": (
        "antipsychotic", "antipsychotique", "neuroleptic", "neuroleptique",
        "dopamine antagonist", "antagoniste dopaminergique", "haloperidol",
        "risperidone", "olanzapine", "quetiapine", "clozapine", "aripiprazole",
        "chlorpromazine", "levomepromazine", "amisulpride", "sulpiride",
    ),
    "dopamine_agonist": (
        "dopamine agonist", "agoniste dopaminergique", "levodopa", "l-dopa",
        "pramipexole", "ropinirole", "bromocriptine", "cabergoline",
        "amantadine",
    ),
    "cyp_inhibitor": (
        "cyp inhibitor", "inhibiteur du cyp", "inhibiteur cyp",
        "inhibiteur enzymatique", "fluoxetine", "paroxetine", "fluvoxamine",
        "clotrimazole", "ketoconazole", "itraconazole", "ritonavir",
        "ciprofloxacin", "quinidine", "pamplemousse", "grapefruit",
        "cimetidine",
    ),
    "cyp_inducer": (
        "cyp inducer", "inducteur du cyp", "inducteur enzymatique",
        "rifampicine", "rifampin", "carbamazepine", "phenytoin",
        "phenobarbital", "millepertuis", "st john",
    ),
    "qt": (
        "allongement du qt", "qt prolongation", "long qt", "sotalol",
        "amiodarone", "methadone", "haloperidol", "ondansetron", "citalopram",
        "escitalopram", "chloroquine", "hydroxychloroquine", "macrolide",
        "fluoroquinolone", "moxifloxacin", "thioridazine", "droperidol",
        "domperidone", "cisapride",
    ),
    "hepatotoxic": (
        "hepatotoxic", "hepatotoxique", "hepatotoxicity", "paracetamol",
        "acetaminophen", "valproate", "valproic acid", "methotrexate",
        "amiodarone", "isoniazid", "isoniazide", "kava",
    ),
    "nephrotoxic": (
        "nephrotoxic", "nephrotoxique", "aminoglycoside", "vancomycin",
        "cisplatin", "cisplatine", "nsaid", "ains",
        "anti-inflammatoire non steroidien", "ibuprofen", "ibuprofene",
        "diclofenac", "naproxen", "ketorolac", "indomethacin", "meloxicam",
    ),
    "anticholinergic": (
        "anticholinergic", "anticholinergique", "antimuscarinic", "atropine",
        "scopolamine", "benztropine", "trihexyphenidyl", "amitriptyline",
        "diphenhydramine", "oxybutynin", "tolterodine",
    ),
    "antiplatelet": (
        "antiplatelet", "antiplaquettaire", "aspirin", "aspirine",
        "clopidogrel", "prasugrel", "ticagrelor", "dipyridamole",
    ),
    "anticoagulant": (
        "anticoagulant", "warfarin", "warfarine", "acenocoumarol",
        "phenprocoumon", "rivaroxaban", "apixaban", "edoxaban", "dabigatran",
        "heparin", "heparine", "enoxaparin",
    ),
    "nsaid": (
        "nsaid", "ains", "nonsteroidal anti-inflammatory", "ibuprofen",
        "ibuprofene", "naproxen", "naproxene", "diclofenac", "indomethacin",
        "ketorolac", "celecoxib", "meloxicam", "piroxicam",
    ),
    "serotonergic_depletor": (
        "reserpine", "reserpine", "tetrabenazine", "deutetrabenazine",
    ),
}

_CNS_TRAITS = {"depressant", "gabaergic", "opioid", "antihistamine",
               "dissociative"}
_SEROTONIN_TRAITS = {"serotonergic", "serotonin_releaser", "psychedelic"}
_STIMULANT_TRAITS = {"stimulant", "sympathomimetic"}


def molecule_traits(name: str, classes=None) -> set[str]:
    """Traits pharmacologiques approx. deduits du nom et des classes connues."""
    parts = [_norm_name(name)]
    for value in (classes or []):
        parts.append(_norm_name(str(value)))
    blob = " | ".join(part for part in parts if part)
    traits: set[str] = set()
    if not blob:
        return traits
    for trait, keywords in _TRAIT_KEYWORDS.items():
        for keyword in keywords:
            folded = _norm_name(keyword)
            if folded and folded in blob:
                traits.add(trait)
                break
    return traits


def _mechanism_key(a: str, b: str) -> str:
    ka, kb = _pair_key(a, b)
    return f"{ka}+{kb}"


def mechanisms_path(cache_dir: Path | None = None) -> Path:
    return (Path(cache_dir) if cache_dir else ddinter_cache_dir()) / (
        "mechanisms.json"
    )


def load_mechanisms(cache_dir: Path | None = None) -> dict:
    """Table utilisateur des mecanismes, ou {}.

    Format : {"lsd+sertraline": {"label": "Potentialisation", "note": "..."}}
    (cle = deux noms normalises tries, joints par « + »).
    """
    try:
        data = json.loads(mechanisms_path(cache_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def workspace_classes(root: Path) -> dict[str, list[str]]:
    """{nom normalise: [classes chimiques/psychoactives]} d'apres les resumes."""
    out: dict[str, list[str]] = {}
    for entry in molecule_catalogue(root):
        values: list[str] = []
        for key in ("chemical_class", "psychoactive_class", "cross_tolerance",
                    "drug_class"):
            for value in (entry.get(key) or []):
                text = str(value).strip()
                if text:
                    values.append(text)
        if values:
            out[_norm_name(entry["name"])] = values
    return out


def _mechanism_rule(ta: set[str], tb: set[str]) -> tuple[str, str]:
    """(cle de mecanisme, explication) deduits des traits ; ("", "") sinon."""
    both = ta & tb
    union = ta | tb

    # 1. IMAO : potentialisation (serotoninergique / sympathomimetique).
    if "maoi" in union:
        other = tb if "maoi" in ta else ta
        if other & (_SEROTONIN_TRAITS | {"stimulant", "sympathomimetic"}):
            return ("potentiation",
                    "IMAO avec une substance sérotoninergique ou "
                    "sympathomimétique : risque de syndrome sérotoninergique "
                    "ou de poussée hypertensive.")
        if other & _CNS_TRAITS:
            return ("potentiation",
                    "IMAO avec un dépresseur du SNC : sédation et effets "
                    "systémiques potentialisés.")
    # 2. deux substances serotoninergiques : addition
    if (ta & _SEROTONIN_TRAITS) and (tb & _SEROTONIN_TRAITS):
        return ("additive",
                "Deux substances agissant sur la sérotonine : effets additifs "
                "possibles (syndrome sérotoninergique à surveiller).")
    # 3. antagoniste opioide face a un depresseur
    if "opioid_antagonist" in (ta | tb):
        other = tb if "opioid_antagonist" in ta else ta
        if other & _CNS_TRAITS:
            return ("antagonism",
                    "Antagoniste opioïde avec un dépresseur du SNC : effet "
                    "antagonisé (syndrome de sevrage possible).")
    # 4. antipsychotique face a un agoniste dopaminergique / stimulant
    if "antipsychotic" in (ta | tb):
        other = tb if "antipsychotic" in ta else ta
        if other & {"dopamine_agonist", "psychedelic", "stimulant"}:
            return ("antagonism",
                    "Antipsychotique (antagoniste dopaminergique) avec un "
                    "agoniste ou un psychostimulant : antagonisme fonctionnel.")
    # 5. deux depresseurs du SNC : synergie
    if (ta & _CNS_TRAITS) and (tb & _CNS_TRAITS):
        return ("synergy",
                "Deux dépresseurs du système nerveux central : sédation et "
                "dépression respiratoire additives.")
    # 6. deux stimulants / sympathomimetiques : synergie
    if (ta & _STIMULANT_TRAITS) and (tb & _STIMULANT_TRAITS):
        return ("synergy",
                "Deux stimulants / sympathomimétiques : stimulation nerveuse "
                "et cardiovasculaire additive.")
    # 7. voie metabolique : inhibiteur ou inducteur enzymatique (CYP)
    if (ta | tb) & {"cyp_inhibitor", "cyp_inducer"}:
        if {"cyp_inhibitor", "cyp_inducer"} <= (ta | tb):
            note = ("Inhibiteur et inducteur enzymatiques (CYP) : "
                    "concentrations plasmatiques imprévisibles.")
        elif "cyp_inducer" in (ta | tb):
            note = ("Inducteur enzymatique (CYP) : concentration de l'autre "
                    "substance abaissée (effet diminué).")
        else:
            note = ("Inhibiteur enzymatique (CYP) : concentration de l'autre "
                    "substance augmentée (surdosage possible).")
        return ("metabolism", note)
    # 8. toxicites organiques et hemorragiques (additives)
    if both & {"qt"}:
        return ("toxicity",
                "Addition sur l'intervalle QT : risque de trouble du rythme "
                "cardiaque.")
    if (ta | tb) & {"antiplatelet", "anticoagulant"} and (
            "nsaid" in (ta | tb)
            or {"antiplatelet", "anticoagulant"} <= (ta | tb)):
        return ("toxicity",
                "Association antiplaquettaire/anticoagulante : risque "
                "hémorragique accru.")
    if both & {"hepatotoxic"}:
        return ("toxicity", "Hépatotoxicité potentiellement additive.")
    if both & {"nephrotoxic"}:
        return ("toxicity", "Toxicité rénale potentiellement additive.")
    if both & {"anticholinergic"}:
        return ("additive",
                "Charge anticholinergique additive (confusion, rétention, "
                "sécheresse).")
    return ("", "")


def interaction_mechanism(a: str, b: str, level: str = "",
                          classes_a=None, classes_b=None,
                          mechanisms: dict | None = None) -> tuple[str, str, str]:
    """(cle, libelle, detail) du mecanisme probable de la paire (a, b).

    Une entree de `mechanisms` (fichier utilisateur) prime sur l'heuristique.
    """
    override = (mechanisms or {}).get(_mechanism_key(a, b))
    if isinstance(override, dict):
        label = str(override.get("label") or override.get("libelle") or "").strip()
        note = str(override.get("note") or override.get("detail") or "").strip()
        if label:
            return ("manuel", label,
                    note or "Mécanisme fixé dans mechanisms.json.")
    elif isinstance(override, str) and override.strip():
        return ("manuel", override.strip(),
                "Mécanisme fixé dans mechanisms.json.")

    ta = molecule_traits(a, classes_a)
    tb = molecule_traits(b, classes_b)
    key, note = _mechanism_rule(ta, tb)
    if key:
        return (key, MECHANISM_LABELS.get(key, key), note)
    traits = ", ".join(sorted(ta | tb)) or "aucun trait identifié"
    if level_rank(level) >= 2:
        return ("unknown", MECHANISM_LABELS["unknown"],
                "Interaction signalée (gravité) sans mécanisme déduit des "
                f"traits retenus ({traits}).")
    return ("unknown", MECHANISM_LABELS["unknown"],
            f"Aucun mécanisme déduit des traits retenus ({traits}).")


def build_interaction_pairs(entries, ddinter: dict | None = None,
                            wd_pairs: dict | None = None,
                            synonyms: dict | None = None,
                            classes: dict | None = None,
                            mechanisms: dict | None = None) -> list[dict]:
    """Croise la liste de molecules et retourne une ligne par paire.

    `entries` : [{"name": str, "text": interactions PsychonautWiki (option)}].
    `ddinter` : index de `ddinter_index()` (ou None).
    `wd_pairs` : {(cle_a, cle_b): detail} issu de `wikidata_interactions()`.
    `classes` : {nom normalise: [classes]} pour le mecanisme probable.
    `mechanisms` : surcharges utilisateur (fichier mechanisms.json).
    Chaque ligne : a, b, level, level_label, rank, sources, known, detail,
    mechanism, mechanism_key, mechanism_note.
    """
    entries = [e for e in (entries or []) if str(e.get("name", "")).strip()]
    wd_pairs = wd_pairs or {}
    dd_names: dict[str, str] = (
        resolve_ddinter_names([e["name"] for e in entries], ddinter, synonyms)
        if ddinter else {}
    )
    rows: list[dict] = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i], entries[j]
            na, nb = str(a["name"]), str(b["name"])
            found: dict[str, str] = {}
            if ddinter and dd_names.get(na) and dd_names.get(nb):
                level = ddinter_pair_level(ddinter, na, nb)
                if level:
                    found["DDInter"] = level
            for src, dst in ((a, b), (b, a)):
                text = str(src.get("text") or "")
                if text and _mentions(text, dst["name"]):
                    found[f"PsychonautWiki ({src['name']})"] = _snippet(
                        text, dst["name"])
            key = _pair_key(na, nb)
            if key in wd_pairs:
                found["Wikidata"] = str(wd_pairs[key] or "")
            level = ""
            for source, value in found.items():
                candidate = value if source == "DDInter" else "Mention"
                if level_rank(candidate) > level_rank(level):
                    level = candidate
            details = []
            for source, value in found.items():
                if not value:
                    details.append(source)
                elif source == "DDInter":
                    details.append(f"{source} : {level_label(value)}")
                else:
                    details.append(f"{source} : {value}")
            mkey, mlabel, mnote = interaction_mechanism(
                na, nb, level,
                (classes or {}).get(_norm_name(na)),
                (classes or {}).get(_norm_name(nb)),
                mechanisms,
            )
            rows.append({
                "a": na,
                "b": nb,
                "level": level,
                "level_label": level_label(level) if level else "Aucune",
                "rank": level_rank(level),
                "sources": " + ".join(found),
                "known": bool(found),
                "detail": " | ".join(details),
                "mechanism": mlabel,
                "mechanism_key": mkey,
                "mechanism_note": mnote,
            })
    rows.sort(key=lambda row: (-row["rank"], _norm_name(row["a"]),
                               _norm_name(row["b"])))
    return rows


def interaction_rows_to_csv(path: Path, rows) -> Path:
    """Ecrit les paires en CSV (« ; » et BOM : lisible par Excel francais)."""
    path = Path(path)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["molecule_a", "molecule_b", "niveau", "mecanisme",
                         "mecanisme_detail", "sources", "detail"])
        for row in rows or []:
            writer.writerow([
                row.get("a", ""), row.get("b", ""), row.get("level_label", ""),
                row.get("mechanism", ""), row.get("mechanism_note", ""),
                row.get("sources", ""), row.get("detail", ""),
            ])
    return path


# -- Wikidata : propriete « interaction significative » (P769) -----------------
def wikidata_cache_path(cache_dir: Path | None = None) -> Path:
    return (Path(cache_dir) if cache_dir else ddinter_cache_dir()) / (
        "wikidata_p769.json"
    )


def load_wikidata_cache(cache_dir: Path | None = None) -> dict:
    """Cache local : {"updated": date, "pairs": {(a, b): detail}}."""
    try:
        raw = json.loads(
            wikidata_cache_path(cache_dir).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {"updated": 0.0, "pairs": {}}
    pairs: dict[tuple[str, str], str] = {}
    for item in raw.get("pairs") or []:
        if isinstance(item, dict):
            a, b = str(item.get("a", "")), str(item.get("b", ""))
            if a and b:
                pairs[(a, b)] = str(item.get("label", ""))
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            pairs[(str(item[0]), str(item[1]))] = str(item[2])
    return {"updated": float(raw.get("updated") or 0.0), "pairs": pairs}


def save_wikidata_cache(updated: float, pairs: dict,
                        cache_dir: Path | None = None) -> None:
    payload = {
        "updated": float(updated),
        "pairs": [[a, b, label] for (a, b), label in sorted(pairs.items())],
    }
    path = wikidata_cache_path(cache_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _wikidata_request(url: str, params: dict, accept: str = "application/json"):
    """GET Wikidata, avec une reprise unique si le debit est limite (429).

    Ces services renvoient parfois « 429 Too Many Requests » : on retente une
    fois apres une courte attente, puis on abandonne proprement (AcquireError)
    pour que la page continue avec les sources locales.
    """
    last_error: Optional[Exception] = None
    for attempt in range(2):
        try:
            resp = requests.get(
                url, params=params, timeout=HTTP_TIMEOUT,
                headers={"User-Agent": rm.USER_AGENT, "Accept": accept},
            )
            if resp.status_code == 429 and attempt == 0:
                time.sleep(3.0)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(3.0)
                continue
            break
    raise AcquireError(f"Wikidata injoignable : {last_error}")


def wikidata_search_qid(name: str, languages=("fr", "en")) -> str:
    """Identifiant Wikidata (Q…) correspondant a `name`, ou ""."""
    for language in languages:
        data = _wikidata_request(WIKIDATA_API, {
            "action": "wbsearchentities", "search": str(name),
            "language": language, "uselang": "fr", "format": "json",
            "limit": 5,
        })
        for hit in data.get("search") or []:
            qid = str(hit.get("id") or "")
            if qid.startswith("Q"):
                return qid
    return ""


def wikidata_p769_edges(qids) -> list[tuple[str, str]]:
    """Relations P769 entre les identifiants donnes (une seule requete)."""
    qids = [str(q) for q in (qids or []) if str(q).startswith("Q")]
    if len(qids) < 2:
        return []
    values = " ".join(f"wd:{q}" for q in qids)
    query = (
        "SELECT ?a ?b WHERE { VALUES ?a { " + values + " } "
        "{ ?a wdt:P769 ?b . } UNION { ?b wdt:P769 ?a . } }"
    )
    data = _wikidata_request(
        WIKIDATA_SPARQL, {"query": query, "format": "json"},
        accept="application/sparql-results+json",
    )
    edges: list[tuple[str, str]] = []
    for binding in (data.get("results") or {}).get("bindings") or []:
        a = str(binding.get("a", {}).get("value", "")).rsplit("/", 1)[-1]
        b = str(binding.get("b", {}).get("value", "")).rsplit("/", 1)[-1]
        if a.startswith("Q") and b.startswith("Q"):
            edges.append((a, b))
    return edges


def wikidata_interactions(names, cache_dir: Path | None = None, progress=None,
                          use_cache: bool = True) -> dict[tuple[str, str], str]:
    """Paires P769 reliant les molecules de `names` (cle = `_pair_key`).

    Une seule requete SPARQL pour toute la liste ; resultat mis en cache
    `WIKIDATA_TTL`. Leve AcquireError si Wikidata est injoignable.
    """
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if len(names) < 2:
        return {}
    now = time.time()
    if use_cache:
        cache = load_wikidata_cache(cache_dir)
        if now - float(cache.get("updated", 0)) < WIKIDATA_TTL:
            wanted = {_pair_key(a, b)
                      for i, a in enumerate(names) for b in names[i + 1:]}
            cached = cache.get("pairs") or {}
            if wanted and wanted <= set(cached):
                return {key: cached[key] for key in wanted}
    else:
        cache = {"updated": 0.0, "pairs": {}}

    if progress:
        progress("Wikidata : identification des molécules…")
    by_qid: dict[str, str] = {}
    for name in names:
        qid = wikidata_search_qid(name)
        if qid:
            by_qid.setdefault(qid, name)
    if len(by_qid) < 2:
        return {}
    if progress:
        progress("Wikidata : lecture des interactions documentées…")
    found: dict[tuple[str, str], str] = {}
    for qa, qb in wikidata_p769_edges(sorted(by_qid)):
        na, nb = by_qid.get(qa), by_qid.get(qb)
        if not na or not nb or na == nb:
            continue
        found.setdefault(_pair_key(na, nb), "interaction documentée (P769)")
    merged = dict(cache.get("pairs") or {})
    merged.update(found)
    if use_cache:
        save_wikidata_cache(now, merged, cache_dir)
    return found
