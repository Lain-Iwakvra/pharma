#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recherche_molecule.py
---------------------

Coeur de parsing psychopharmacologique : **module de la bibliothèque de
l'application**, pas un script autonome. Inspiré de l'approche de pw-cli
(https://github.com/pharmacologic/pw-cli). Le seul point d'entrée du projet
est `pharma_studio.py` (application de bureau).

Pour une molecule donnee, `build_report()` :
  1. Interroge l'API GraphQL de PsychonautWiki (Bifrost,
     https://api.psychonautwiki.org) pour recuperer des donnees structurees :
        classification, noms communs, potentiel addictif, toxicite,
        tolerance, interactions, effets, et pour chaque voie
        d'administration (ROA) : dosage (seuil/leger/commun/fort/lourd),
        duree (apparition/come-up/pic/offset/total), biodisponibilite.
  2. Recupere la page MediaWiki de PsychonautWiki (wikitext) pour completer
     les champs absents de GraphQL (notamment la demi-vie / half-life).
  3. Recupere la page Wikipedia en langue anglaise (API MediaWiki, librairie
     de Goldsmith si disponible, sinon appel direct) pour completer
     (statut legal, toxicite detaillee, chimie...).
  4. Fusionne les informations pharmacologiques les plus importantes :
     dosage, voie(s) d'administration, temps d'apparition (onset), pic
     plasmatique (Tmax / peak), demi-vie, biodisponibilite, duree d'action,
     effets residuels, toxicite, potentiel addictif, statut legal.

Les donnees PsychonautWiki sont prioritaires (wiki specialise dans les
produits de recherche), completees par Wikipedia quand necessaire.

Ce module fournit uniquement des fonctions et des modeles : aucun affichage
console, aucun parsing de ligne de commande. L'interface graphique (dialogue
de recherche, ambiguïtés, export) appartient à `pharma_studio.py`, qui
délègue ici le téléchargement et le fusionnement des sources.

Exports les plus utilisés par l'application :
    build_report, save_json, save_markdown, report_rows, build_unified_rows,
    WikiClient, resolve_title, AmbiguousResults, MoleculeReport,
    fold_accents, LABELS_FR, TARGET_ORDER, COMPARE_ORDER, USER_AGENT
"""

from __future__ import annotations

import html as html_mod
import json
import re
import textwrap
import unicodedata
from dataclasses import dataclass, field, asdict, fields
from functools import lru_cache
from typing import Callable, Optional
from urllib.parse import quote

import requests

# --- dependances optionnelles ------------------------------------------------
try:
    import wikipedia as wp_lib  # librairie de Goldsmith (API Wikipedia)
    HAS_WP_LIB = True
except Exception:  # pragma: no cover - dependance optionnelle
    HAS_WP_LIB = False

try:
    from bs4 import BeautifulSoup  # parse HTML des infoboxes Wikipedia
    HAS_BS4 = True
except Exception:  # pragma: no cover - dependance optionnelle
    HAS_BS4 = False


# --- endpoints ---------------------------------------------------------------
PW_GRAPHQL = "https://api.psychonautwiki.org/"          # Bifrost (GraphQL)
PW_MEDIAWIKI = "https://psychonautwiki.org/w/api.php"   # MediaWiki PsychonautWiki
EN_WIKI_MEDIAWIKI = "https://en.wikipedia.org/w/api.php"  # MediaWikipedia anglais

USER_AGENT = (
    "PharmaResearch/1.0 (educational research; contact: research@example.local)"
)
REQUEST_TIMEOUT = 30

MAX_SEARCH_RESULTS = 8

# Requetes GraphQL (inspirees de pw-cli)
SEARCH_GRAPHQL_QUERY = """
query SubstanceSearch($query: String!, $limit: Int!) {
  substances(query: $query, limit: $limit) {
    name
    url
    summary
    class { chemical psychoactive }
  }
}
"""

SUBSTANCE_GRAPHQL_QUERY = """
query SubstanceInfo($query: String!) {
  substances(query: $query, limit: 1) {
    name
    url
    summary
    featured
    class { chemical psychoactive }
    commonNames
    systematicName
    addictionPotential
    toxicity
    tolerance { full half zero }
    crossTolerances
    effects { name url }
    dangerousInteractions { name }
    unsafeInteractions { name }
    uncertainInteractions { name }
    roas {
      name
      dose {
        units
        threshold
        heavy
        common { min max }
        light { min max }
        strong { min max }
      }
      duration {
        afterglow { min max units }
        comeup { min max units }
        duration { min max units }
        offset { min max units }
        onset { min max units }
        peak { min max units }
        total { min max units }
      }
      bioavailability { min max }
    }
  }
}
"""

# Cibles d'information pharmacologique et leurs libelles d'affichage
LABELS_FR: dict[str, str] = {
    "name": "Nom",
    "common_names": "Noms communs",
    "systematic_name": "Nom systématique",
    "chemical_class": "Classification chimique",
    "psychoactive_class": "Classification psychoactive",
    "routes": "Voie(s) d'administration",
    "dosage": "Dosage",
    "onset": "Temps d'apparition (onset)",
    "comeup": "Montée (come-up)",
    "peak": "Pic plasmatique (Tmax / peak)",
    "duration": "Durée d'action (totale)",
    "offset": "Redescente / offset",
    "bioavailability": "Biodisponibilité",
    "halflife": "Demi-vie (half-life)",
    "protein_binding": "Liaison protéique (protein binding)",
    "metabolism": "Métabolisme (metabolism)",
    "metabolites": "Métabolites (metabolites)",
    "addiction": "Potentiel addictif / dépendance",
    "tolerance": "Tolérance",
    "cross_tolerance": "Tolérance croisée",
    "toxicity": "Toxicité",
    "effects": "Effets rapportés",
    "interactions": "Interactions",
    "legal": "Statut légal",
}

# Libelles courts des sous-champs d'une voie d'administration (ROA).
# Source unique pour l'application web, l'atelier de bureau et la CLI.
ROA_PART_LABELS: dict[str, str] = {
    "dosage": "Dosage",
    "onset": "Apparition",
    "comeup": "Come-up",
    "peak": "Pic (Tmax)",
    "duration": "Durée",
    "offset": "Offset",
    "bioavailability": "Biodisponibilité",
}

# Ordre d'affichage des champs pharmacologiques fusionnes.
# "legal" est volontairement absent : c'est une donnee purement Wikipedia,
# deja affichee par la section [Legal status] de l'infobox (aucun doublon).
TARGET_ORDER: list[str] = [
    "name", "common_names", "systematic_name", "chemical_class",
    "psychoactive_class", "routes", "dosage", "onset", "comeup", "peak",
    "duration", "offset", "bioavailability", "halflife", "addiction",
    "tolerance", "cross_tolerance", "toxicity", "effects", "interactions",
]

# Ordre des colonnes du tableau de comparaison (page « Comparaison »).
# Les champs qui n'y figurent pas (voies, duree totale, effets, interactions,
# tolerance, noms secondaires...) restent disponibles dans le resume et pour
# les interactions locales, mais ne sont pas affiches en colonnes.
COMPARE_ORDER: list[str] = [
    "chemical_class", "psychoactive_class", "dosage",
    "halflife", "bioavailability", "protein_binding", "metabolism",
    "metabolites", "onset", "comeup", "peak", "offset", "toxicity",
]

# Cles calculees mais jamais affichees telles quelles (presentes dans le JSON)
HIDDEN_KEYS: set[str] = {
    "summary_pw", "dosage_first", "bioavailability_first", "duration_detail",
}

# Ordre d'affichage des sections de l'infobox Wikipedia (web et CLI),
# du plus au moins pertinent pour la recherche pharmacocinetique.
WIKI_SECTION_ORDER: list[str] = [
    "pharmacokinetic data",
    "clinical data",
    "chemical and physical data",
    "legal status",
    "identifiers",
]

# Libelles FR des sections de l'infobox Wikipedia (affichage CLI et Markdown).
WP_SECTION_FR: dict[str, str] = {
    "Identifiers": "Identifiants",
    "Chemical and physical data": "Données chimiques et physiques",
    "Clinical data": "Données cliniques",
    "Pharmacokinetic data": "Données pharmacocinétiques",
    "Legal status": "Statut légal",
}

# Meme table, indexee en minuscules : le nom des sections varie selon la casse
# renvoyee par le parser (« clinical data » vs « Clinical data »).
_WP_SECTION_FR_LOWER: dict[str, str] = {
    key.lower(): value for key, value in WP_SECTION_FR.items()
}

# Fragments de label infobox ecartes partout (web, CLI, JSON, Markdown) :
# modelisation 3D sans interet pharmacologique.
WP_SKIP_FRAGMENTS: tuple[str, ...] = ("3d model", "jsmol")

# Rang d'une section dans l'ordre d'affichage (cles en minuscules).
WIKI_SECTION_RANK: dict[str, int] = {
    name: i for i, name in enumerate(WIKI_SECTION_ORDER)
}


def wiki_section_label(section_name: str) -> str:
    """Nom de section Wikipedia traduit en francais si le libelle est connu."""
    return _WP_SECTION_FR_LOWER.get((section_name or "").lower(), section_name)


def wiki_section_sort_key(section_name: str) -> tuple[int, str]:
    """Cle de tri des sections : WIKI_SECTION_ORDER, puis ordre alphabetique.

    La comparaison est insensible a la casse car les infoboxes renvoient
    « Pharmacokinetic data » alors que WIKI_SECTION_ORDER est en minuscules.
    """
    low = (section_name or "").lower()
    return (WIKI_SECTION_RANK.get(low, len(WIKI_SECTION_ORDER)), low)


def _wp_label_filtered(label: str) -> bool:
    """Vrai si le label doit etre ecarte (modelisation 3D, etc.)."""
    low = (label or "").lower()
    return any(frag in low for frag in WP_SKIP_FRAGMENTS)


def fold_accents(text: str) -> str:
    """Retire les accents d'une chaine (comparaisons insensibles aux accents).

    « Statut légal » -> « Statut legal », « Duree » -> « Duree ».
    """
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in decomposed if not unicodedata.combining(c))

# Libelles fusionnes dont certaines sections de l'infobox Wikipedia sont des
# redites (ex. "Other names" reprend "Noms communs"). Cle = libelle fusionne,
# valeur = fragments (en minuscules) retrouves dans le libelle Wikipedia.
LABEL_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "common_names": ("other names", "noms communs", "synonym"),
    "addiction": ("addiction liability", "dependence liability",
                  "addiction", "dependance"),
    "legal": ("legal status", "statut legal"),
}


@lru_cache(maxsize=512)
def canonical_label(label: str) -> str:
    """Retourne la cle canonique ('routes', 'legal'...) d'un libelle, ou ''.

    La comparaison est insensible a la casse **et aux accents**, pour que
    « Statut légal » (libelle affiche) et « legal status » (infobox anglaise)
    designent bien le meme champ.
    """
    low = fold_accents(label or "").lower()
    for cible, fragments in LABEL_EQUIVALENTS.items():
        if any(fold_accents(frag).lower() in low for frag in fragments):
            return cible
    return ""


# --- modeles de donnees ------------------------------------------------------
@dataclass
class WikiInfobox:
    """Contenu structuré d'une infobox Wikipedia."""

    fields: dict[str, str] = field(default_factory=dict)
    # champs hors section (label -> valeur)

    sections: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # nom de section -> [(label, valeur), ...]


@dataclass
class PwRoa:
    """Voie d'administration (route of administration) venant de GraphQL."""
    name: str = ""
    dose: str = ""        # texte formate (seuil/leger/commun/fort/lourd + unites)
    onset: str = ""       # temps d'apparition
    comeup: str = ""      # come-up
    peak: str = ""        # pic plasmatique (Tmax)
    duration: str = ""    # duree totale d'action
    offset: str = ""      # redescente / offset
    bioavailability: str = ""  # texte formate (min-max %)


@dataclass
class PwSubstance:
    """Substance PsychonautWiki donnee par l'API GraphQL."""
    name: str = ""
    url: str = ""
    summary: str = ""
    featured: bool = False
    chemical_class: list[str] = field(default_factory=list)
    psychoactive_class: list[str] = field(default_factory=list)
    common_names: list[str] = field(default_factory=list)
    systematic_name: str = ""
    addiction_potential: str = ""
    toxicity: list[str] = field(default_factory=list)
    tolerance_full: str = ""
    tolerance_half: str = ""
    tolerance_zero: str = ""
    cross_tolerances: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)
    roas: list[PwRoa] = field(default_factory=list)
    interactions_dangerous: list[str] = field(default_factory=list)
    interactions_uncertain: list[str] = field(default_factory=list)
    interactions_unsafe: list[str] = field(default_factory=list)
    found: bool = False
    error: Optional[str] = None


@dataclass
class PageSource:
    """Description d'une page MediaWiki recuperee."""
    title: str = ""
    url: str = ""
    wikitext: str = ""
    summary: str = ""
    html: str = ""
    found: bool = False
    error: Optional[str] = None


@dataclass
class MoleculeReport:
    molecule: str
    pw_substance: PwSubstance = field(default_factory=PwSubstance)
    pw_page: PageSource = field(default_factory=PageSource)
    wikipedia: PageSource = field(default_factory=PageSource)
    wiki_infobox: WikiInfobox = field(default_factory=WikiInfobox)
    # infobox Wikipedia structuree (sections + champs hors section)
    pharmacology: dict[str, dict[str, str]] = field(default_factory=dict)
    # pharmacology[champ] = {"value": ..., "source": "pw"/"wikipedia"/"both",
    #                        "pw": ..., "wiki": ...}

    def to_dict(self) -> dict:
        return {
            "molecule": self.molecule,
            "psychonautwiki_graphql": asdict(self.pw_substance),
            "psychonautwiki_page": asdict(self.pw_page),
            "wikipedia_page": asdict(self.wikipedia),
            "wikipedia_infobox": asdict(self.wiki_infobox),
            "pharmacology": self.pharmacology,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MoleculeReport":
        """Reconstruit un rapport depuis un `resume.json` (voir `to_dict`).

        Sert a reafficher un resume dont le HTML manque, **sans reseau** :
        les champs absents ou d'un type inattendu sont simplement ignores.
        """
        data = data if isinstance(data, dict) else {}
        return cls(
            molecule=str(data.get("molecule", "") or ""),
            pw_substance=_dataclass_from(
                PwSubstance, data.get("psychonautwiki_graphql")
            ),
            pw_page=_dataclass_from(
                PageSource, data.get("psychonautwiki_page")
            ),
            wikipedia=_dataclass_from(PageSource, data.get("wikipedia_page")),
            wiki_infobox=_dataclass_from(
                WikiInfobox, data.get("wikipedia_infobox")
            ),
            pharmacology=_pharmacology_from_dict(data.get("pharmacology")),
        )


def _dataclass_from(cls, payload):
    """Instancie `cls` depuis un dictionnaire JSON, en ignorant l'inconnu.

    Seul le champ `roas` (liste de `PwRoa`) demande une relecture recurrente.
    """
    if not isinstance(payload, dict):
        return cls()
    kwargs = {}
    for spec in fields(cls):
        if spec.name not in payload:
            continue
        value = payload[spec.name]
        if spec.name == "roas":
            if isinstance(value, list):
                kwargs[spec.name] = [
                    _dataclass_from(PwRoa, item)
                    for item in value
                    if isinstance(item, dict)
                ]
            continue
        kwargs[spec.name] = value
    try:
        return cls(**kwargs)
    except TypeError:
        return cls()


def _pharmacology_from_dict(payload) -> dict:
    """Normalise `pharmacology` : dict[champ] -> dict[str, str]."""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            out[str(key)] = {str(k): str(v) for k, v in value.items()}
    return out


# --- client GraphQL PsychonautWiki (Bifrost) ---------------------------------
class PsychonautGraphQLClient:
    """Client GraphQL pour l'API Bifrost de PsychonautWiki (inspire de pw-cli)."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        )

    def _execute(self, query: str, variables: dict) -> dict:
        try:
            resp = self.session.post(
                PW_GRAPHQL,
                json={"query": query, "variables": variables},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"Erreur GraphQL HTTP: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"Reponse GraphQL JSON invalide: {exc}") from exc
        if "errors" in data:
            raise RuntimeError(f"Erreurs GraphQL: {data['errors']}")
        return data.get("data", {})

    def search(self, query: str, limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
        data = self._execute(
            SEARCH_GRAPHQL_QUERY, {"query": query, "limit": limit}
        )
        return data.get("substances", [])

    def get_substance(self, name: str) -> Optional[dict]:
        data = self._execute(SUBSTANCE_GRAPHQL_QUERY, {"query": name})
        subs = data.get("substances", [])
        return subs[0] if subs else None


def _norm_roa_name(name: str) -> str:
    """Nom de voie lisible en francais."""
    mapping = {
        "oral": "Orale",
        "sublingual": "Sublinguale",
        "buccal": "Buccale",
        "insufflated": "Insufflee (nez)",
        "rectal": "Rectale",
        "transdermal": "Transdermique",
        "subcutaneous": "Sous-cutanée",
        "intramuscular": "Intramusculaire",
        "intravenous": "Intraveineuse",
        "smoked": "Fumée",
        "vaporized": "Vaporisée",
    }
    return mapping.get((name or "").strip().lower(), (name or "inconnue").capitalize())


def _format_dose(dose: Optional[dict], units: Optional[str]) -> str:
    """Formate un bloc dose GraphQL en texte lisible."""
    if not dose:
        return ""
    levels = [
        ("seuil", "threshold"),
        ("léger", "light"),
        ("commun", "common"),
        ("fort", "strong"),
        ("lourd", "heavy"),
    ]
    parts = []
    for label, key in levels:
        val = dose.get(key)
        if isinstance(val, dict):
            mn, mx = val.get("min"), val.get("max")
            if mn is not None and mx is not None:
                parts.append(f"{label}: {mn}-{mx}")
            elif mn is not None:
                parts.append(f"{label}>={mn}")
        elif val is not None:
            parts.append(f"{label}: {val}")
    if units:
        parts.append(f"({units})")
    return " ; ".join(parts)


def _dur_field(dur: Optional[dict], key: str) -> str:
    """Extrait un seul champ d'un bloc duration GraphQL."""
    if not dur:
        return ""
    val = dur.get(key)
    if isinstance(val, dict):
        mn, mx, u = val.get("min"), val.get("max"), val.get("units")
        if mn is not None and mx is not None:
            return f"{mn}-{mx} {u or ''}".strip()
        if mn is not None:
            return f">={mn} {u or ''}".strip()
    return ""


def _format_duration_full(dur: Optional[dict]) -> str:
    """Formate l'ensemble d'un bloc duration GraphQL."""
    if not dur:
        return ""
    order = [
        ("apparition", "onset"),
        ("come-up", "comeup"),
        ("pic", "peak"),
        ("durée", "duration"),
        ("offset", "offset"),
        ("après-effets", "afterglow"),
        ("totale", "total"),
    ]
    parts = []
    for label, key in order:
        v = _dur_field(dur, key)
        if v:
            parts.append(f"{label}: {v}")
    return "; ".join(parts)


def _format_bioavailability(bio: Optional[dict]) -> str:
    if not bio:
        return ""
    mn, mx = bio.get("min"), bio.get("max")
    if mn is None or mx is None:
        return ""
    if mn == mx:
        return f"{mn}%"
    return f"{mn}-{mx}%"


def parse_pw_substance(raw: dict) -> PwSubstance:
    """Convertit la reponse GraphQL brute en PwSubstance."""
    s = PwSubstance()
    if not raw:
        return s
    s.name = raw.get("name", "")
    s.url = raw.get("url", "")
    s.summary = raw.get("summary", "")
    s.featured = bool(raw.get("featured", False))
    cls = raw.get("class") or {}
    s.chemical_class = cls.get("chemical") or []
    s.psychoactive_class = cls.get("psychoactive") or []
    s.common_names = raw.get("commonNames") or []
    s.systematic_name = raw.get("systematicName", "")
    s.addiction_potential = raw.get("addictionPotential", "")
    s.toxicity = raw.get("toxicity") or []
    tol = raw.get("tolerance") or {}
    s.tolerance_full = tol.get("full", "") or ""
    s.tolerance_half = tol.get("half", "") or ""
    s.tolerance_zero = tol.get("zero", "") or ""
    s.cross_tolerances = raw.get("crossTolerances") or []
    s.effects = [e.get("name", "") for e in (raw.get("effects") or []) if e.get("name")]

    # interactions (pw-cli recupere dangerous/uncertain/unsafe)
    s.interactions_dangerous = [
        i.get("name", "") for i in (raw.get("dangerousInteractions") or []) if i.get("name")
    ]
    s.interactions_uncertain = [
        i.get("name", "") for i in (raw.get("uncertainInteractions") or []) if i.get("name")
    ]
    s.interactions_unsafe = [
        i.get("name", "") for i in (raw.get("unsafeInteractions") or []) if i.get("name")
    ]

    for r in raw.get("roas") or []:
        roa = PwRoa()
        roa.name = _norm_roa_name(r.get("name", ""))
        dose = r.get("dose") or {}
        roa.dose = _format_dose(dose, dose.get("units"))
        dur = r.get("duration") or {}
        roa.onset = _dur_field(dur, "onset")
        roa.comeup = _dur_field(dur, "comeup")
        roa.peak = _dur_field(dur, "peak")
        roa.duration = _dur_field(dur, "duration")
        roa.offset = _dur_field(dur, "offset")
        roa.bioavailability = _format_bioavailability(r.get("bioavailability"))
        s.roas.append(roa)

    s.found = True
    return s


# --- client MediaWiki commun -------------------------------------------------
class WikiClient:
    """Client minimal pour une API MediaWiki (PsychonautWiki ou Wikipedia)."""

    def __init__(self, api_url: str, site_url: str, site_name: str) -> None:
        self.api_url = api_url
        self.site_url = site_url
        self.site_name = site_name
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _get(self, params: dict) -> dict:
        try:
            resp = self.session.get(
                self.api_url, params=params, timeout=REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"Erreur HTTP ({self.site_name}): {exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"Reponse JSON invalide ({self.site_name}): {exc}") from exc

    def search(self, query: str, limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
        data = self._get({
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": limit,
            "format": "json",
            "formatversion": 2,
        })
        return data.get("query", {}).get("search", [])

    def parse_wikitext(self, title: str) -> str:
        data = self._get({
            "action": "parse",
            "page": title,
            "prop": "wikitext",
            "format": "json",
            "formatversion": 2,
        })
        return self._extract_prop(data, "wikitext")

    def parse_html(self, title: str) -> str:
        data = self._get({
            "action": "parse",
            "page": title,
            "prop": "text",
            "format": "json",
            "formatversion": 2,
        })
        return self._extract_prop(data, "text")

    @staticmethod
    def _extract_prop(data: dict, prop: str) -> str:
        parse = data.get("parse", {})
        val = parse.get(prop)
        if isinstance(val, dict):
            return val.get("*", "")
        return val or ""

    def page_url(self, title: str) -> str:
        encoded = quote(title.replace(" ", "_"), safe="")
        return f"{self.site_url}/wiki/{encoded}"


# --- choix d'un titre / d'une substance --------------------------------------
class AmbiguousResults(Exception):
    """Plusieurs resultats possibles et aucun choix fourni par l'appelant.

    L'exception porte la liste des candidats : la CLI la transforme en menu
    numéroté dans le terminal, l'application de bureau en boutons dans une
    fenêtre de dialogue. Cette bibliotheque ne lit donc jamais `input()`
    elle-meme.
    """

    def __init__(self, source: str, source_name: str, query: str,
                 candidates: list[str]):
        super().__init__(f"Plusieurs résultats sur {source_name} pour '{query}'")
        self.source = source                  # cle courte : "pw" / "wp"
        self.source_name = source_name        # libelle affiche a l'utilisateur
        self.query = query
        self.candidates = list(candidates)


def resolve_title(titles: list[str], query: str, source_name: str,
                  source_key: str, choice: str = "") -> str:
    """Choisit un titre parmi `titles` sans jamais deviner.

    Priorite : choix explicite fourni par l'appelant, puis correspondance
    exacte avec la requete. Sinon `AmbiguousResults` est levee pour que
    l'appelant demande a l'utilisateur (terminal ou fenetre de dialogue).
    """
    if not titles:
        return ""
    # Un seul candidat : il n'y a rien a choisir, pas d'ecran inutile.
    if len(titles) == 1:
        return titles[0]
    wanted = (choice or "").strip().lower()
    if wanted:
        for title in titles:
            if title.strip().lower() == wanted:
                return title
    target = query.strip().lower()
    for title in titles:
        if title.strip().lower() == target:
            return title
    raise AmbiguousResults(source_key, source_name, query, titles)


def choose_title(results: list[dict], query: str, source_name: str,
                 source_key: str = "pw", choice: str = "") -> Optional[dict]:
    """Variante de resolve_title pour des resultats au format dictionnaire."""
    if not results:
        return None
    titles = [str(r.get("title", r.get("name", ""))) for r in results]
    picked = resolve_title(titles, query, source_name, source_key, choice)
    for r in results:
        if str(r.get("title", r.get("name", ""))) == picked:
            return r
    return results[0]


# --- parsage des encadr MediaWiki (wikitext) ---------------------------------
def _normalize_key(key: str) -> str:
    return re.sub(r"[\s_\-:]+", "", key or "").lower().strip()


FIELD_RULES: list[tuple[str, list[str]]] = [
    ("halflife", [
        "halflife", "half-life", "half life", "eliminationhalflife",
        "eliminationhalf-life", "elimination half life",
        "plasma halflife", "plasmathird",
    ]),
    ("routes", ["routeofadministration", "routes", "route", "administration"]),
    ("dosage", [
        "dosage", "usualdosage", "recommendeddosage", "dose", "doses",
        "threshold", "lightdose", "commondose", "strongdose", "heavydose",
    ]),
    ("onset", ["onset", "onsettoaction", "onsetofaction", "timeofonset", "onsettime"]),
    ("peak", ["peak", "tmax", "timetopeak", "time to peak", "peakplasma"]),
    ("duration", ["duration", "durationofaction", "totalduration"]),
    ("bioavailability", ["bioavailability", "oralbioavailability"]),
    ("protein_binding", [
        "proteinbinding", "protein binding", "plasmaproteinbinding",
    ]),
    ("metabolism", ["metabolism", "metabolizedby", "metabolicpathways"]),
    ("metabolites", ["metabolites", "activemetabolites"]),
    ("toxicity", ["toxicity", "lethaldose", "ld50", "overdose"]),
    ("addiction", ["addictionpotential", "dependencepotential", "addiction"]),
    ("legal", ["legalstatus", "legality", "legal status"]),
]


def _find_template(text: str, hints: tuple[str, ...]) -> Optional[int]:
    idx = 0
    n = len(text)
    low_hints = [h.lower() for h in hints]
    while True:
        start = text.find("{{", idx)
        if start == -1:
            return None
        j = start + 2
        while j < n and text[j] in " \t\r\n":
            j += 1
        k = j
        while k < n and text[k] not in "}|:\n\t\r ":
            k += 1
        name = text[j:k]
        if any(h in name.lower() for h in low_hints):
            return start
        idx = start + 2


def parse_template_params(text: str, start: int) -> dict[str, str]:
    params: dict[str, str] = {}
    n = len(text)
    i = start + 2
    while i < n and text[i] in " \t\r\n":
        i += 1
    while i < n and text[i] not in "|}":
        i += 1
    depth = 1
    while i < n:
        c = text[i]
        if c == "{" and i + 1 < n and text[i + 1] == "{":
            depth += 1
            i += 2
            continue
        if c == "}" and i + 1 < n and text[i + 1] == "}":
            depth -= 1
            i += 2
            if depth == 0:
                break
            continue
        if c == "|" and depth == 1:
            i += 1
            key_start = i
            while i < n:
                if text[i] == "{" and i + 1 < n and text[i + 1] == "{":
                    depth += 1
                    i += 2
                    continue
                if text[i] == "}" and i + 1 < n and text[i + 1] == "}":
                    depth -= 1
                    i += 2
                    if depth == 0:
                        break
                    continue
                if text[i] == "=" and depth == 1:
                    break
                if text[i] == "|" and depth == 1:
                    break
                i += 1
            key = text[key_start:i].strip()
            if i >= n or text[i] != "=":
                if key:
                    params[key] = ""
                continue
            i += 1
            val_start = i
            while i < n:
                if text[i] == "{" and i + 1 < n and text[i + 1] == "{":
                    depth += 1
                    i += 2
                    continue
                if text[i] == "}" and i + 1 < n and text[i + 1] == "}":
                    depth -= 1
                    i += 2
                    if depth == 0:
                        break
                    continue
                if text[i] == "|" and depth == 1:
                    break
                i += 1
            params[key] = text[val_start:i]
        else:
            i += 1
    return params


def extract_infobox_params(
    wikitext: str,
    hints: tuple[str, ...] = ("drug", "pharm", "chembox", "infobox"),
) -> dict[str, str]:
    if not wikitext:
        return {}
    start = _find_template(wikitext, hints)
    if start is None:
        return {}
    return parse_template_params(wikitext, start)


# --- nettoyage wikitext ------------------------------------------------------
def _strip_templates(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\{\{[^{}]*\}\}", "", text)
        text = re.sub(r"\{\{[^{}]*\{\{[^{}]*\}\}[^{}]*\}\}", "", text)
    return text


def truncate(value: str, max_len: int = 450) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 3].rstrip() + "..."


def clean_wikitext(value: str) -> str:
    if not value:
        return ""
    text = value
    text = _strip_templates(text)
    text = re.sub(r"\[\[([^|\]]*\|)?([^\]]*)\]\]", r"\2", text)
    text = re.sub(r"<ref[^>]*>.*?", "", text, flags=re.S)
    text = re.sub(r"<ref[^>]*/>", "", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(
        r"</?(?:sup|sub|b|i|u|em|strong|code|span|div|p)[^>]*>",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"={2,6}\s*[^=]*?\s*={2,6}", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"\b[A-Za-z0-9_éèàùêâôîûëïüöç ]+::\s*", " ", text)
    text = re.sub(r"\[https?://[^\s\]]*([^\]]*)\]", r"\1", text)
    text = re.sub(r"(?:^|\s)\*\s*", " ", text, flags=re.M)
    text = text.replace("\n", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def map_infobox(raw_params: dict[str, str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for key, value in raw_params.items():
        cible = None
        nkey = _normalize_key(key)
        for target, mots in FIELD_RULES:
            for mot in mots:
                m = _normalize_key(mot)
                if nkey == m or m in nkey or nkey in m:
                    cible = target
                    break
            if cible:
                break
        if not cible or cible in mapped:
            continue
        cleaned = clean_wikitext(value)
        if cleaned:
            mapped[cible] = cleaned
    return mapped


def extract_section(wikitext: str, titles: list[str], max_len: int = 1200) -> str:
    if not wikitext:
        return ""
    for title in titles:
        pattern = re.compile(
            r"={2,3}\s*" + re.escape(title) + r"\s*={2,3}(.*?)(?=\n={2,3}\s|\Z)",
            re.S | re.I,
        )
        m = pattern.search(wikitext)
        if m:
            cleaned = clean_wikitext(m.group(1))
            if cleaned:
                return truncate(cleaned, max_len)
    return ""


SCAN_PATTERNS: dict[str, list[str]] = {
    "halflife": [
        r"half-?life\s*(?:of\s*)?[:=]\s*([^\n;,]{5,160})",
        r"half-?life\s+of\s+([^\n;,]{5,130})",
        r"elimination\s+half-?life\s*[:=]\s*([^\n;,]{5,160})",
    ],
    "onset": [r"onset\s*[:=]\s*([^\n;,]{5,160})"],
    "peak": [r"peak\s*[:=]\s*([^\n;,]{5,160})", r"\btmax\b\s*[:=]\s*([^\n;,]{5,90})"],
    "duration": [r"duration\s*[:=]\s*([^\n;,]{5,160})"],
    "bioavailability": [r"bioavailability\s*[:=]\s*([^\n;,]{5,160})"],
    "dosage": [r"dosage\s*[:=]\s*([^\n;,]{5,180})"],
}


def scan_text(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for cible, patterns in SCAN_PATTERNS.items():
        if cible in found:
            continue
        for pat in patterns:
            m = re.search(pat, text, re.I | re.S)
            if m:
                val = clean_wikitext(m.group(1))
                if val and len(val) >= 3:
                    found[cible] = truncate(val, 300)
                    break
    return found


# --- Wikipedia (anglais) -----------------------------------------------------
def fetch_wikipedia(query: str, choice: str = "") -> PageSource:
    """Recupere la page Wikipedia anglaise (lib Goldsmith puis API directe).

    `choice` porte un choix deja fait par l'utilisateur : il est utilise tel
    quel et aucune question n'est re-demandee.
    """
    page = PageSource(title="", url="")

    def build(title: str, wikitext: str, summary: str, html: str = "") -> PageSource:
        p = PageSource()
        p.title = title
        p.url = f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='')}"
        p.wikitext = wikitext
        p.summary = summary
        p.html = html
        p.found = True
        return p

    lib_error: Optional[Exception] = None
    if HAS_WP_LIB:
        try:
            wp_lib.set_lang("en")
            try:
                wp_lib.set_user_agent(USER_AGENT)
            except Exception:
                pass
            # Choix deja pris par l'utilisateur : on vise la page demandee
            # sans relancer de recherche (le meme arbitrage ne serait pas
            # redemande a l'infini).
            if choice:
                chosen_title = choice
            else:
                results = wp_lib.search(query, results=MAX_SEARCH_RESULTS)
                if not results:
                    page.error = "Aucun résultat sur Wikipedia (anglais)."
                    return page
                chosen_title = resolve_title(
                    results, query, "Wikipedia (anglais)", "wp"
                )
            page_obj = wp_lib.page(chosen_title, auto_suggest=False)
            return build(
                page_obj.title,
                page_obj.content or "",
                page_obj.summary or "",
                page_obj.html() if HAS_BS4 else "",
            )
        except AmbiguousResults:
            raise
        except wp_lib.DisambiguationError as exc:
            # Page-tiroir : au lieu de prendre la premiere option en silence,
            # on la presente a l'utilisateur (terminal ou application).
            options = list(exc.options or [])
            if not options:
                page.error = "Page Wikipedia ambiguë et sans option claire."
                return page
            pick = choice if choice in options else None
            if pick is None:
                raise AmbiguousResults(
                    "wp", "Wikipedia (anglais)", query, options
                ) from exc
            try:
                page_obj = wp_lib.page(pick, auto_suggest=False)
                return build(
                    page_obj.title,
                    page_obj.content or "",
                    page_obj.summary or "",
                    page_obj.html() if HAS_BS4 else "",
                )
            except Exception as inner:
                lib_error = inner
        except Exception as exc:
            lib_error = exc

    client = WikiClient(EN_WIKI_MEDIAWIKI, "https://en.wikipedia.org", "Wikipedia (anglais)")
    try:
        if choice:
            title = choice
        else:
            results = client.search(query)
            chosen = choose_title(results, query, "Wikipedia (anglais)", "wp")
            if not chosen:
                if not page.error:
                    page.error = "Aucun résultat sur Wikipedia (anglais)." + (
                        f" (lib: {lib_error})" if lib_error else ""
                    )
                return page
            title = chosen["title"]
        wikitext = client.parse_wikitext(title)
        html = client.parse_html(title)
        summary = ""
        m = re.search(r"^(.*?)(?=\n\n|\n==)", wikitext, re.S)
        if m:
            summary = clean_wikitext(m.group(1))
        return build(title, wikitext, summary, html)
    except AmbiguousResults:
        raise
    except Exception as exc:
        if not page.error:
            page.error = str(exc) if not lib_error else f"{lib_error}; {exc}"
        return page


# --- assemblage du profil pharmacologique ------------------------------------
def _first_nonempty(values: list[str]) -> str:
    for v in values:
        if v:
            return v
    return ""


def pharmacology_from_graphql(sub: PwSubstance) -> dict[str, str]:
    """Transforme les donnees GraphQL PsychonautWiki en champs cibles."""
    out: dict[str, str] = {}
    if sub.name:
        out["name"] = sub.name
    if sub.common_names:
        out["common_names"] = "; ".join(sub.common_names)
    if sub.systematic_name:
        out["systematic_name"] = sub.systematic_name
    if sub.chemical_class:
        out["chemical_class"] = "; ".join(sub.chemical_class)
    if sub.psychoactive_class:
        out["psychoactive_class"] = "; ".join(sub.psychoactive_class)

    # voies d'administration
    if sub.roas:
        out["routes"] = "; ".join(r.name for r in sub.roas)

    # dosage : aggregation par route (toutes les ROA qui ont une dose)
    dose_parts = []
    for r in sub.roas:
        if r.dose:
            dose_parts.append(f"{r.name}: {r.dose}")
    if dose_parts:
        out["dosage"] = "; ".join(dose_parts)
        out["dosage_first"] = _first_nonempty([r.dose for r in sub.roas if r.dose])

    # champs pharmacocinétiques simples (valeur de la premiere ROA renseignée)
    out["onset"] = _first_nonempty([r.onset for r in sub.roas if r.onset])
    out["comeup"] = _first_nonempty([r.comeup for r in sub.roas if r.comeup])
    out["peak"] = _first_nonempty([r.peak for r in sub.roas if r.peak])
    out["duration"] = _first_nonempty([r.duration for r in sub.roas if r.duration])
    out["offset"] = _first_nonempty([r.offset for r in sub.roas if r.offset])

    # biodisponibilite
    bio_parts = []
    for r in sub.roas:
        if r.bioavailability:
            bio_parts.append(f"{r.name}: {r.bioavailability}")
    if bio_parts:
        out["bioavailability"] = "; ".join(bio_parts)
        out["bioavailability_first"] = _first_nonempty(
            [r.bioavailability for r in sub.roas if r.bioavailability]
        )

    # duree complete par route (affichage detaille)
    dur_parts = []
    for r in sub.roas:
        bits = []
        if r.onset:
            bits.append(f"apparition {r.onset}")
        if r.comeup:
            bits.append(f"come-up {r.comeup}")
        if r.peak:
            bits.append(f"pic {r.peak}")
        if r.duration:
            bits.append(f"duree {r.duration}")
        if r.offset:
            bits.append(f"offset {r.offset}")
        if bits:
            dur_parts.append(f"{r.name}: " + "; ".join(bits))
    if dur_parts:
        out["duration_detail"] = "; ".join(dur_parts)

    if sub.addiction_potential:
        out["addiction"] = sub.addiction_potential
    tol_parts = []
    if sub.tolerance_full:
        tol_parts.append(f"pleine: {sub.tolerance_full}")
    if sub.tolerance_half:
        tol_parts.append(f"demi: {sub.tolerance_half}")
    if sub.tolerance_zero:
        tol_parts.append(f"zéro: {sub.tolerance_zero}")
    if tol_parts:
        out["tolerance"] = "; ".join(tol_parts)
    if sub.cross_tolerances:
        out["cross_tolerance"] = "; ".join(sub.cross_tolerances)
    if sub.toxicity:
        out["toxicity"] = "; ".join(sub.toxicity)
    if sub.effects:
        out["effects"] = "; ".join(sub.effects)
    inter_parts = []
    if sub.interactions_dangerous:
        inter_parts.append("dangereuses: " + "; ".join(sub.interactions_dangerous))
    if sub.interactions_unsafe:
        inter_parts.append("déconseillées: " + "; ".join(sub.interactions_unsafe))
    if sub.interactions_uncertain:
        inter_parts.append("à confirmer: " + "; ".join(sub.interactions_uncertain))
    if inter_parts:
        out["interactions"] = "; ".join(inter_parts)
    if sub.summary:
        out["summary_pw"] = sub.summary
    return out


def parse_wikipedia_infobox_html(html: str) -> WikiInfobox:
    """Extrait uniquement l'infobox Wikipedia cible depuis le HTML parsé.

    Cible la balise demandee :
        <table class="infobox" style="border-spacing:2px;" about="...">
    L'attribut about etant dynamique, on filtre sur la classe 'infobox'
    associee au style border-spacing:2px, avec fallback sur la premiere
    table infobox trouvee.

    L'infobox pharmaceutique Wikipedia est organisee en sections
    (<th colspan="2">Nom de section</th>) suivies de lignes
    (<th>Label</th><td>Valeur</td>). Cette fonction capture toutes les
    sections, notamment :
        Clinical data / Donnees cliniques
        Pharmacokinetic data / Donnees pharmacocinétiques
        Legal status / Statut legal
        Identifiers / Identifiants
        Chemical and physical data / Donnees chimiques et physiques
        ... et toute autre section presente.

    Retourne un WikiInfobox avec :
        - fields : champs hors section (label -> valeur)
        - sections : nom_section -> [(label, valeur), ...]
    """
    result = WikiInfobox()
    if not html or not HAS_BS4:
        return result
    soup = BeautifulSoup(html, "html.parser")

    target = None
    for table in soup.find_all("table"):
        classes = table.get("class", [])
        if isinstance(classes, str):
            classes = [classes]
        if "infobox" not in classes:
            continue
        style = (table.get("style") or "").replace(" ", "")
        if "border-spacing:2px" in style:
            target = table
            break
    if target is None:
        # fallback : premiere table portant la classe infobox
        for table in soup.find_all("table"):
            classes = table.get("class", [])
            if isinstance(classes, str):
                classes = [classes]
            if "infobox" in classes:
                target = table
                break
    if target is None:
        return result

    current_section: Optional[str] = None
    for tr in target.find_all("tr"):
        ths = tr.find_all("th", recursive=False)
        tds = tr.find_all("td", recursive=False)

        # En-tête de section : un seul <th> sans <td> (souvent colspan="2")
        if len(ths) == 1 and len(tds) == 0:
            section_name = ths[0].get_text(" ", strip=True)
            if section_name:
                current_section = section_name
                result.sections.setdefault(current_section, [])
            continue

        # Ligne de donnees : au moins un <th> et un <td>
        if len(ths) >= 1 and len(tds) >= 1:
            label = ths[0].get_text(" ", strip=True)
            value = tds[0].get_text(" ", strip=True)
            if not label or not value:
                continue
            if _wp_label_filtered(label):
                continue  # modelisation 3D sans interet pharmacologique
            # nettoyer les references [1], [ 2 ], etc. et espaces multiples
            value = re.sub(r"\s*\[\s*\d+\s*\]\s*", " ", value)
            value = re.sub(r"\s+", " ", value).strip()

            entry = (label, value)
            if current_section:
                result.sections.setdefault(current_section, []).append(entry)
            else:
                if label not in result.fields:
                    result.fields[label] = value

    return result


def _wiki_label_to_cible(label: str) -> Optional[str]:
    """Mappe un label d'infobox Wikipedia vers un champ cible connu."""
    nkey = _normalize_key(label)
    for cible, mots in FIELD_RULES:
        for mot in mots:
            m = _normalize_key(mot)
            if nkey == m or m in nkey or nkey in m:
                return cible
    return None


# Sections wikitext scannees quand l'encadree ne suffit pas.
SECTION_TITLES: dict[str, list[str]] = {
    "dosage": ["dosage", "dose", "doses"],
    "onset": ["onset", "time of onset"],
    "peak": ["peak", "tmax", "time to peak", "peak plasma"],
    "duration": ["duration", "duration of action"],
    "halflife": ["half-life", "halflife", "elimination half-life"],
    "bioavailability": ["bioavailability"],
    "aftereffects": ["after effects", "aftereffects", "comedown"],
    "toxicity": ["toxicity", "overdose", "ld50"],
    "addiction": ["addiction", "dependence", "tolerance"],
    "legal": ["legal status", "legality"],
}


def build_pharmacology(
    sub: PwSubstance,
    pw_text: str,
    wiki_text: str,
    wiki_infobox: WikiInfobox,
) -> dict[str, dict[str, str]]:
    """Fusionne GraphQL PW, scan wikitext PW et infobox Wikipedia."""
    pw_fields = pharmacology_from_graphql(sub)
    pw_raw = extract_infobox_params(pw_text)
    pw_infobox = map_infobox(pw_raw)
    # ne pas ecraser les valeurs GraphQL par l'infobox PW, seulement completer
    for k, v in pw_infobox.items():
        pw_fields.setdefault(k, v)

    # complement par scan des sections quand l'encadree est muette
    for cible, titles in SECTION_TITLES.items():
        if cible not in pw_fields:
            val = extract_section(pw_text, titles)
            if val:
                pw_fields[cible] = val

    # dernier recours: scan textuel par motifs sur le corps des pages
    pw_scan = scan_text(pw_text)
    for cible, val in pw_scan.items():
        pw_fields.setdefault(cible, val)

    # Wikipedia : infobox HTML (fields plats), infobox wikitext, scan
    wiki_raw = extract_infobox_params(wiki_text)
    wiki_infobox_wt = map_infobox(wiki_raw)
    wiki_scan = scan_text(wiki_text)
    wiki_fields: dict[str, str] = {}
    for k, v in wiki_infobox.fields.items():
        wiki_fields.setdefault(k, v)
    for k, v in wiki_infobox_wt.items():
        wiki_fields.setdefault(k, v)
    for k, v in wiki_scan.items():
        wiki_fields.setdefault(k, v)

    # Wikipedia : lignes des encadres en sections (ex. « Pharmacokinetic
    # data » : bioavailability, protein binding, metabolism, half-life...).
    # Ces tableaux ne passent pas par les champs plats ni par le wikitext
    # quand l'infobox est rendue en sections.
    for rows in wiki_infobox.sections.values():
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                continue
            label, value = row
            if not str(label).strip() or not str(value).strip():
                continue
            for key, val in map_infobox({str(label): str(value)}).items():
                wiki_fields.setdefault(key, val)

    result: dict[str, dict[str, str]] = {}
    # champs cibles a afficher, dans l'ordre desire (TARGET_ORDER)
    # legal vient uniquement de Wikipedia (scan/infobox)
    all_keys = list(dict.fromkeys(
        list(pw_fields.keys()) + list(wiki_fields.keys()) + TARGET_ORDER
    ))
    for key in all_keys:
        pw_val = pw_fields.get(key, "")
        wiki_val = wiki_fields.get(key, "")
        if not pw_val and not wiki_val:
            continue
        if pw_val and wiki_val:
            source = "both"
            value = pw_val
        elif pw_val:
            source = "psychonautwiki"
            value = pw_val
        else:
            source = "wikipedia"
            value = wiki_val
        result[key] = {
            "value": value,
            "source": source,
            "pw": pw_val,
            "wiki": wiki_val,
        }
    return result


# --- lignes unifiees du tableau (web, bureau, export) ------------------------
def build_unified_rows(
    sub: PwSubstance,
    wiki_infobox: WikiInfobox,
    pharmacology: dict[str, dict[str, str]],
) -> list[dict]:
    """Construit une liste de lignes unifiees (sans redondance) pour le tableau.

    Chaque ligne est un dictionnaire :
      {"label": str, "value": str, "parts": list|None}
    - value : texte simple affiche dans la cellule
    - parts : liste de (sous_libelle, valeur) pour les voies d'administration,
              rendue par l'appelant (aucun HTML injecte ici)

    Regle de dedoublement : si une valeur (apres normalisation) est deja
    presente sous un libelle different, on ne cree pas de nouvelle ligne. On
    garde le libelle le plus court et la valeur la plus complete.
    """
    rows: list[dict] = []

    def _norm_value(v: str) -> str:
        v = v.lower()
        v = re.sub(r"[^\w\s]", " ", v)
        v = re.sub(r"\s+", " ", v).strip()
        return v

    def _values_similar(a: str, b: str, threshold: float = 0.72) -> bool:
        na, nb = _norm_value(a), _norm_value(b)
        if not na or not nb:
            return False
        la, lb = len(na), len(nb)
        # l'un contient l'autre : seulement si la valeur contenue est assez
        # longue pour etre significative (evite "80%" dans "80 to 85 C")
        if na in nb or nb in na:
            shorter, longer = (na, nb) if la < lb else (nb, na)
            # une valeur courte contenue dans une plus longue n'est pas un
            # doublon : "Oxycodone" dans "US DailyMed : Oxycodone" doit
            # rester une ligne distincte.
            if len(shorter) < 6:
                return False
            if min(la, lb) / max(la, lb) < 0.50:
                return False
            return True
        # similitude de Jaccard sur les mots, avec contrainte de longueur
        if min(la, lb) / max(la, lb) < 0.30:
            return False
        sa, sb = set(na.split()), set(nb.split())
        if not sa or not sb:
            return False
        return len(sa & sb) / len(sa | sb) >= threshold

    def _find_similar_index(value: str) -> Optional[int]:
        for i, r in enumerate(rows):
            if _values_similar(r["value"], value):
                return i
        return None

    def add(label: str, value: str, parts=None) -> None:
        if not value and not parts:
            return
        # texte servant a la comparaison de deduplication
        compare_value = value or " ".join(f"{k} {v}" for k, v in (parts or []))
        idx = _find_similar_index(compare_value)
        if idx is not None:
            existing = rows[idx]
            # ne pas empiler les libelles : garder le plus court (le plus
            # significatif, ex. "Nom" plutot qu'une chaine de libelles fusionnes)
            if len(label) < len(existing["label"]):
                existing["label"] = label
            if len(value) > len(existing["value"]):
                existing["value"] = value
            return
        rows.append({"label": label, "value": value, "parts": parts})

    # --- 1. Informations generales (depuis pharmacology fusionne) ---
    displayed_fused: set[str] = set()
    for key in ("name", "common_names", "systematic_name",
                "chemical_class", "psychoactive_class"):
        if key in pharmacology:
            add(LABELS_FR.get(key, key), pharmacology[key]["value"])
            displayed_fused.add(key)

    # --- 2. Voies d'administration (ROA) depuis PsychonautWiki ---
    # Une voie par ligne : dosage, timings et biodisponibilite y figurent deja.
    # Le titre porte deja le nom de la voie : pas de ligne "Voie(s)" redondante.
    roa_value_keys: set[str] = set()
    if sub.found and sub.roas:
        for r in sub.roas:
            parts = []
            for key, value in (
                ("dosage", r.dose),
                ("onset", r.onset),
                ("comeup", r.comeup),
                ("peak", r.peak),
                ("duration", r.duration),
                ("offset", r.offset),
                ("bioavailability", r.bioavailability),
            ):
                if value:
                    parts.append((ROA_PART_LABELS[key], value))
            if parts:
                summary = "; ".join(f"{k}: {v}" for k, v in parts)
                add(f"Voie d'administration — {r.name}", summary, parts=parts)
        # Ces valeurs sont deja rendues voie par voie ci-dessus : la section 3
        # ne doit pas les redoubler (sinon biodisponibilite/timings en double).
        roa_value_keys = {
            "dosage", "onset", "comeup", "peak", "duration", "offset",
            "bioavailability",
        }
        displayed_fused.add("routes")

    # --- 3. Champs pharmacologiques fusionnes (dosage, demi-vie, etc.) ---
    general_keys = {"name", "common_names", "systematic_name",
                    "chemical_class", "psychoactive_class"}
    for key in TARGET_ORDER:
        if key in general_keys or key not in pharmacology:
            continue
        if key in displayed_fused:
            continue
        data = pharmacology[key]
        lbl = LABELS_FR.get(key, key)
        pw = data.get("pw", "") or ""
        wiki = data.get("wiki", "") or ""
        source = data.get("source", "")
        if key in roa_value_keys:
            if source == "both" and wiki and wiki != pw:
                add(lbl, wiki)
            continue
        if source == "both" and pw and wiki and pw != wiki:
            add(lbl, pw)
            if key != "halflife":
                add(lbl, wiki)
        else:
            add(lbl, pw or wiki or data.get("value", ""))
        displayed_fused.add(key)

    # --- 4. Sections Wikipedia complementaires (non deja couvertes) ---
    # Les labels sans interet (modelisation 3D, ...) ont deja ete ecartes par
    # parse_wikipedia_infobox_html (WP_SKIP_FRAGMENTS) ; l'ordre des sections
    # vient de wiki_section_sort_key (WIKI_SECTION_ORDER).
    for section_name in sorted(wiki_infobox.sections, key=wiki_section_sort_key):
        for label, value in wiki_infobox.sections[section_name]:
            canon = canonical_label(label)
            if canon and canon in displayed_fused:
                continue
            add(label, value)

    for label, value in wiki_infobox.fields.items():
        canon = canonical_label(label)
        if canon and canon in displayed_fused:
            continue
        add(label, value)

    return rows


# --- recupereurs de pages ----------------------------------------------------
def fetch_psychonautwiki(query: str, choice: str = "") -> tuple[PwSubstance, PageSource]:
    """Recupere la substance via GraphQL (donnees structurees) et sa page
    MediaWiki (wikitext pour les scans complementaires).

    `choice` porte un choix deja fait par l'utilisateur sur les resultats
    GraphQL : il est utilise tel quel, sans nouvelle question.
    """
    sub = PwSubstance()
    page = PageSource(title="", url="")

    client = PsychonautGraphQLClient()
    try:
        if choice:
            name = choice
        else:
            results = client.search(query)
            if not results:
                sub.error = "Aucun résultat sur PsychonautWiki (GraphQL)."
                page.error = sub.error
                return sub, page
            chosen = choose_title(results, query, "PsychonautWiki (GraphQL)", "pw")
            name = chosen["name"]
        raw = client.get_substance(name)
        if not raw:
            sub.error = "Aucune donnée structurée retournée par GraphQL."
            page.error = sub.error
            return sub, page
        sub = parse_pw_substance(raw)
        page.title = sub.name
        page.url = sub.url or f"https://psychonautwiki.org/wiki/{quote(name.replace(' ', '_'), safe='')}"
        page.found = True
    except AmbiguousResults:
        # pas une erreur : l'appelant (CLI ou application) doit trancher
        raise
    except Exception as exc:
        sub.error = str(exc)
        page.error = str(exc)
        return sub, page

    # recuperer aussi le wikitext PW pour scans complementaires (half-life, etc.)
    mw = WikiClient(PW_MEDIAWIKI, "https://psychonautwiki.org", "PsychonautWiki")
    try:
        page.wikitext = mw.parse_wikitext(page.title)
        try:
            page.html = mw.parse_html(page.title)
        except Exception:
            page.html = ""
    except Exception as exc:
        # pas bloquant : on a deja les donnees GraphQL
        page.error = (page.error or "") + f" | wikitext: {exc}"

    return sub, page


# --- rapport complet (pipeline partage) --------------------------------------
def build_report(molecule: str,
                 choices: Optional[dict[str, str]] = None,
                 progress: "Optional[Callable[[str], None]]" = None
                 ) -> MoleculeReport:
    """Recupere les sources et assemble le rapport complet d'une molecule.

    Pipeline commun a la ligne de commande et a l'application de bureau.
    Peut lever AmbiguousResults : c'est alors a l'appelant de faire choisir
    l'utilisateur puis de rappeler avec `choices`.

    `progress` recoit des messages d'avancement (utile pour une barre de
    statut) ; par defaut, la fonction reste silencieuse.
    """
    choices = choices or {}

    def _say(message: str) -> None:
        if progress is not None:
            progress(message)

    _say(f"Recherche de '{molecule}' sur PsychonautWiki (GraphQL Bifrost) ...")
    report = MoleculeReport(molecule=molecule)
    report.pw_substance, report.pw_page = fetch_psychonautwiki(
        molecule, choices.get("pw", "")
    )
    _say(f"Recherche de '{molecule}' sur Wikipedia (anglais) ...")
    report.wikipedia = fetch_wikipedia(molecule, choices.get("wp", ""))
    report.wiki_infobox = parse_wikipedia_infobox_html(report.wikipedia.html)
    report.pharmacology = build_pharmacology(
        report.pw_substance,
        report.pw_page.wikitext,
        report.wikipedia.wikitext,
        report.wiki_infobox,
    )
    return report


def report_rows(report: MoleculeReport) -> list[dict]:
    """Lignes unifiees du tableau pour un rapport deja construit."""
    return build_unified_rows(
        report.pw_substance, report.wiki_infobox, report.pharmacology
    )


# --- sauvegarde --------------------------------------------------------------
def save_json(report: MoleculeReport, path: str, quiet: bool = False) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
    if not quiet:
        print(f"[ok] JSON sauvegarde : {path}")


def _esc(value: str) -> str:
    return value.replace("|", "\\|")


def _md_pharma_row(lines: list[str], label: str, data: dict[str, str]) -> None:
    """Ajoute une ligne (ou deux) au tableau Markdown selon la regle :
    - donnee presente sur un seul site : une ligne
    - donnee sur les deux sites avec la meme valeur : une ligne
    - donnee sur les deux sites avec des valeurs divergentes : deux lignes
    """
    pw = data.get("pw", "") or ""
    wiki = data.get("wiki", "") or ""
    source = data.get("source", "")
    label_esc = _esc(label)

    if source == "both" and pw and wiki and pw != wiki:
        lines.append(f"| {label_esc} | {_esc(pw)} |")
        lines.append(f"| {label_esc} | {_esc(wiki)} |")
    else:
        value = pw or wiki or data.get("value", "")
        lines.append(f"| {label_esc} | {_esc(value)} |")


def save_markdown(report: MoleculeReport, path: str,
                  quiet: bool = False) -> None:
    sub = report.pw_substance
    wp = report.wikipedia
    infobox = report.wiki_infobox

    lines = [f"# Recherche psychopharmacologique : {report.molecule}\n"]

    # --- sources ---
    lines.append("## Sources\n")
    if sub.found:
        lines.append(f"### 1. PsychonautWiki (GraphQL Bifrost)\n")
        lines.append(f"- **Nom** : {sub.name}")
        lines.append(f"- **URL** : [{sub.name}]({sub.url})")
        if sub.systematic_name:
            lines.append(f"- **Nom systématique** : {sub.systematic_name}")
        if sub.common_names:
            lines.append(f"- **Noms communs** : {'; '.join(sub.common_names)}")
        if sub.summary:
            lines.append(f"- **Résumé** : {sub.summary}")
    else:
        lines.append("### 1. PsychonautWiki (GraphQL Bifrost)\n")
        lines.append("- Non trouvée.")
        if sub.error:
            lines.append(f"- Erreur : {sub.error}")
    lines.append("")

    if wp.found:
        lines.append(f"### 2. Wikipedia (anglais)\n")
        lines.append(f"- **Titre** : [{wp.title}]({wp.url})")
        if wp.summary:
            lines.append(f"- **Résumé** : {wp.summary}")
    else:
        lines.append("### 2. Wikipedia (anglais)\n")
        lines.append("- Non trouvée.")
        if wp.error:
            lines.append(f"- Erreur : {wp.error}")
    lines.append("")

    # --- PW GraphQL ---
    lines.append("## Données structurées PsychonautWiki (GraphQL Bifrost)\n")
    if sub.found:
        if sub.chemical_class:
            lines.append(f"- **Classification chimique** : {'; '.join(sub.chemical_class)}")
        if sub.psychoactive_class:
            lines.append(f"- **Classification psychoactive** : {'; '.join(sub.psychoactive_class)}")
        if sub.systematic_name:
            lines.append(f"- **Nom systématique** : {sub.systematic_name}")
        if sub.common_names:
            lines.append(f"- **Noms communs** : {'; '.join(sub.common_names)}")
        if sub.addiction_potential:
            lines.append(f"- **Potentiel addictif / dépendance** : {sub.addiction_potential}")
        if sub.toxicity:
            lines.append(f"- **Toxicité** : {'; '.join(sub.toxicity)}")
        if sub.effects:
            lines.append(f"- **Effets rapportés** : {'; '.join(sub.effects)}")
        if sub.interactions_dangerous:
            lines.append(f"- **Interactions dangereuses** : {'; '.join(sub.interactions_dangerous)}")
        if sub.interactions_unsafe:
            lines.append(f"- **Interactions déconseillées** : {'; '.join(sub.interactions_unsafe)}")
        if sub.interactions_uncertain:
            lines.append(f"- **Interactions à confirmer** : {'; '.join(sub.interactions_uncertain)}")
        if sub.cross_tolerances:
            lines.append(f"- **Tolérance croisée** : {'; '.join(sub.cross_tolerances)}")

        tol_parts = []
        if sub.tolerance_full:
            tol_parts.append(f"pleine: {sub.tolerance_full}")
        if sub.tolerance_half:
            tol_parts.append(f"demi: {sub.tolerance_half}")
        if sub.tolerance_zero:
            tol_parts.append(f"zéro: {sub.tolerance_zero}")
        if tol_parts:
            lines.append(f"- **Tolérance** : {'; '.join(tol_parts)}")

        lines.append("")
        lines.append("### Voies d'administration (ROA)\n")
        if sub.roas:
            lines.append("| Voie | Dosage | Apparition | Come-up | Pic (Tmax) | Durée | Offset | Biodisponibilité |")
            lines.append("|------|--------|------------|---------|------------|-------|--------|------------------|")
            for r in sub.roas:
                lines.append(
                    f"| {r.name} | {r.dose or '-'} | {r.onset or '-'} | {r.comeup or '-'} | "
                    f"{r.peak or '-'} | {r.duration or '-'} | {r.offset or '-'} | {r.bioavailability or '-'} |"
                )
        else:
            lines.append("- Aucune voie d'administration renseignée.")
    else:
        lines.append("- Aucune donnée structurée PsychonautWiki.")
    lines.append("")

    # --- Wikipedia par section ---
    lines.append("## Wikipedia (anglais) — Infobox par section\n")
    if not infobox.sections and not infobox.fields:
        lines.append("- Aucune infobox Wikipedia exploitable.\n")
    else:
        for sec in sorted(infobox.sections, key=wiki_section_sort_key):
            lines.append(f"### {wiki_section_label(sec)}\n")
            for label, value in infobox.sections[sec]:
                lines.append(f"- **{label}** : {value}")
            lines.append("")
        if infobox.fields:
            lines.append("### Autres champs de l'infobox\n")
            for label, value in infobox.fields.items():
                lines.append(f"- **{label}** : {value}")
            lines.append("")

    # --- pharmacologie fusionnee ---
    lines.append("## Pharmacologie — Champs cibles fusionnés\n")
    if not report.pharmacology:
        lines.append("- Aucune donnée structurée détectée.\n")
    else:
        shown = set()
        lines.append("| Champ | Valeur |")
        lines.append("|-------|--------|")
        for key in TARGET_ORDER:
            if key in report.pharmacology and key not in HIDDEN_KEYS:
                _md_pharma_row(lines, LABELS_FR.get(key, key), report.pharmacology[key])
                shown.add(key)
        for key, data in report.pharmacology.items():
            if key in HIDDEN_KEYS or key in shown:
                continue
            _md_pharma_row(lines, LABELS_FR.get(key, key), data)
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    if not quiet:
        print(f"[ok] Markdown sauvegarde : {path}")


