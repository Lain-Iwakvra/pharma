#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pharma_studio.py
----------------

Atelier de recherche psychopharmacologique (application de bureau PySide6).

Trois volets, facon editeur de code :

  * gauche  : explorateur du repertoire de travail (racine comprise)
  * centre  : lecteur de documents (PDF, texte, HTML, images, EPUB)
  * droite  : resume de la molecule en cours, genere par le parser

Un document herite de la molecule de son dossier ancetre le plus proche
(voir studio_backend.locate_molecule) ; le bouton « + » de l'explorateur
propose de creer des sous-dossiers et d'ajouter : une molecule (dossier avec
PDF Wikipedia + PDF PsychonautWiki + resume), le PDF d'une page Wikipedia ou
PsychonautWiki, un document existant, ou une reference bibliographique
(PubMed, PubMed Central, Internet Archive, Open Library).

Lancement :

    python3 pharma_studio.py                 # dialogue de choix du dossier
    python3 pharma_studio.py --workspace DIR  # dossier impose

Rappel : outil de recherche documentaire. Ni avis medical, ni conseil de
dosage.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import (
    QDir,
    QEvent,
    QMarginsF,
    QMimeData,
    QObject,
    QPointF,
    QRunnable,
    QSettings,
    QSize,
    Qt,
    QSortFilterProxyModel,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QDesktopServices,
    QDrag,
    QKeySequence,
    QPageLayout,
    QPageSize,
    QPixmap,
    QPdfWriter,
    QStandardItem,
    QStandardItemModel,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtPdf import QPdfDocument, QPdfSearchModel
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFileSystemModel,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyle,
    QTabBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QToolBar,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

import recherche_molecule as rm
import studio_backend as sb

ORG_NAME = "pharma-studio"
APP_NAME = "Pharma Studio"
DISCLAIMER = (
    "Outil de recherche documentaire. Les données proviennent de "
    "PsychonautWiki et de Wikipédia : elles ne constituent ni un avis "
    "médical, ni un conseil de dosage."
)

# type MIME des glisser-deposer internes : le chemin depose vers le tableau
PATH_MIME = "application/x-pharma-path"


# --- infrastructure de taches de fond ----------------------------------------
class WorkerSignals(QObject):
    """Signaux emis par un Worker vers le fil principal."""

    message = Signal(str)
    fraction = Signal(float)
    finished = Signal(object)
    failed = Signal(str)
    ambiguous = Signal(object)


class Worker(QRunnable):
    """Execute une fonction longue dans le pool de threads de Qt.

    La fonction recoit deux callbacks : `progress(message)` et
    `fraction(0.0..1.0)`. Les erreurs attendues (choix ambigu, echec reseau)
    sont transformees en signaux plutot qu'en exceptions non gerees.
    """

    def __init__(self, func):
        super().__init__()
        self.func = func
        self.signals = WorkerSignals()
        # le runnable reste possede par Python : evite tout double-free avec
        # le pool de threads, qui ne s'en occupe plus.
        self.setAutoDelete(False)

    @Slot()
    def run(self) -> None:  # pragma: no cover - execute dans un thread
        try:
            result = self.func(self.signals.message.emit, self.signals.fraction.emit)
        except rm.AmbiguousResults as exc:
            self.signals.ambiguous.emit(exc)
        except (sb.WorkspaceError, sb.AcquireError) as exc:
            self.signals.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - dernier rempart de l'UI
            self.signals.failed.emit(f"{type(exc).__name__} : {exc}")
        else:
            self.signals.finished.emit(result)


class Task:
    """Tache relancable : conserve les choix deja faits par l'utilisateur.

    `build(choices, progress, fraction)` peut lever rm.AmbiguousResults : la
    fenetre demande alors le choix a l'utilisateur, l'enregistre dans
    `choices` et relance la meme tache.
    """

    def __init__(self, label: str, build, on_done=None, on_fail=None):
        self.label = label
        self.build = build
        self.on_done = on_done
        self.on_fail = on_fail
        self.choices: dict[str, str] = {}


# --- dialogues ---------------------------------------------------------------
class ChooseTitleDialog(QDialog):
    """Choix d'une page parmi plusieurs resultats (jamais devine a la place)."""

    def __init__(self, parent, exc: rm.AmbiguousResults):
        super().__init__(parent)
        self.setWindowTitle("Choisir un résultat")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"Plusieurs résultats sur <b>{exc.source_name}</b> pour "
            f"« {exc.query} ».<br>Choisissez la page à utiliser :"
        ))

        self.list = QListWidget(self)
        for title in exc.candidates:
            self.list.addItem(QListWidgetItem(title))
        self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.button(QDialogButtonBox.Ok).setText("Utiliser cette page")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choice(self) -> str:
        item = self.list.currentItem()
        return item.text() if item is not None else ""

    @staticmethod
    def ask(parent, exc: rm.AmbiguousResults) -> str:
        """Retourne le titre choisi, ou "" si l'utilisateur annule."""
        dialog = ChooseTitleDialog(parent, exc)
        if dialog.exec() == QDialog.Accepted:
            return dialog.choice()
        return ""


class ChooseMoleculeDialog(QDialog):
    """Nom de molecule a associer a un dossier."""

    def __init__(self, parent, current: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Associer à une molécule")
        layout = QFormLayout(self)
        layout.addRow(QLabel(
            "Le dossier sera relié à cette molécule : son résumé s'affichera\n"
            "dans le volet de droite quand un PDF y sera ouvert."
        ))
        self.field = QLineEdit(current, self)
        self.field.setPlaceholderText("ex. : LSD, MDMA, 3-MMC…")
        layout.addRow("Molécule :", self.field)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def molecule(self) -> str:
        return self.field.text().strip()


class BookSearchDialog(QDialog):
    """Recherche bibliographique multi-sources, puis import du PDF en accès libre.

    Sources : PubMed et PubMed Central (articles), Internet Archive et Open
    Library (livres). Un double-clic tente le téléchargement du PDF ; s'il n'y
    a pas de PDF libre, la page de la référence s'ouvre dans le navigateur.
    """

    SOURCES = (
        ("PubMed (articles scientifiques)", "pubmed"),
        ("PubMed Central (articles en accès libre)", "pmc"),
        ("Internet Archive (livres en accès libre)", "ia"),
        ("Open Library (livres)", "ol"),
    )

    NOTE = ("Uniquement l'accès libre : aucun contenu piraté "
            "(ni Sci-Hub, ni Z-Library).")

    def __init__(self, parent, directory: Path, root: Path | None = None):
        super().__init__(parent)
        self.directory = Path(directory)
        self.root = Path(root) if root else None
        self.downloaded: Path | None = None
        self._hits: list = []
        self._workers: list[Worker] = []
        self.pool = QThreadPool.globalInstance()
        self.settings = QSettings(ORG_NAME, APP_NAME)

        self.setWindowTitle(
            f"Rechercher un article ou un livre — {self.directory.name}"
        )
        self.resize(1000, 600)

        layout = QVBoxLayout(self)
        line = QHBoxLayout()
        self.source = QComboBox(self)
        for label, key in self.SOURCES:
            self.source.addItem(label, key)
        self.source.currentIndexChanged.connect(self._source_changed)
        line.addWidget(self.source)

        self.field = QLineEdit(self)
        self.field.setPlaceholderText(
            "Mots-clés (ex. : psilocybin depression randomized)"
        )
        self.field.returnPressed.connect(self.start_search)
        self.button = QPushButton("Rechercher", self)
        self.button.clicked.connect(self.start_search)
        line.addWidget(self.field, 1)
        line.addWidget(self.button)
        layout.addLayout(line)

        mail_line = QHBoxLayout()
        mail_label = QLabel("E-mail (Unpaywall) :", self)
        self.email = QLineEdit(self)
        self.email.setPlaceholderText(
            "Facultatif : active l'index de PDF libres Unpaywall "
            "(adresse conservée sur cet ordinateur uniquement)"
        )
        self.email.setToolTip(
            "Unpaywall refuse les API anonymes : une adresse de contact "
            "débloque une source de PDF en accès libre supplémentaire."
        )
        self.email.setText(str(self.settings.value("unpaywall_email", "")))
        self.email.editingFinished.connect(self._save_email)
        mail_line.addWidget(mail_label)
        mail_line.addWidget(self.email, 1)
        layout.addLayout(mail_line)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(
            ["Source", "Titre", "Auteur(s)", "Année", "Journal / accès"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemDoubleClicked.connect(lambda _i: self._double_click())
        layout.addWidget(self.table, 1)

        self.status = QLabel(
            "Sources : PubMed, PubMed Central, Internet Archive, Open Library.\n"
            + self.NOTE
        )
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        line2 = QHBoxLayout()
        self.open_button = QPushButton("Ouvrir la page", self)
        self.open_button.clicked.connect(self.open_page)
        self.download_button = QPushButton("Télécharger le PDF", self)
        self.download_button.clicked.connect(self.start_download)
        self.close_button = QPushButton("Fermer", self)
        self.close_button.clicked.connect(self.reject)
        line2.addWidget(self.open_button)
        line2.addWidget(self.download_button)
        line2.addStretch(1)
        line2.addWidget(self.close_button)
        layout.addLayout(line2)
        self._update_buttons()

    # -- recherche -------------------------------------------------------
    @staticmethod
    def _run_search(key: str, query: str, progress) -> list:
        if key == "pubmed":
            return sb.search_pubmed(query, progress=progress)
        if key == "pmc":
            return sb.search_pubmed_central(query)
        if key == "ia":
            return sb.search_internet_archive(query)
        if key == "ol":
            return sb.search_open_library(query)
        raise sb.AcquireError(f"Source de recherche inconnue : {key}")

    def start_search(self) -> None:
        query = self.field.text().strip()
        if not query:
            return
        key = str(self.source.currentData())
        self.button.setEnabled(False)
        self.status.setText(f"Recherche en cours — {self.source.currentText()}…")
        worker = Worker(
            lambda progress, fraction: self._run_search(key, query, progress)
        )
        self._workers.append(worker)
        worker.signals.finished.connect(self._on_results)
        worker.signals.failed.connect(self._on_failed)
        self.pool.start(worker)

    @Slot()
    def _source_changed(self) -> None:
        self._hits = []
        self.table.setRowCount(0)
        self.status.setText(
            f"Source : {self.source.currentText()}.\n{self.NOTE}"
        )
        self._update_buttons()

    @Slot()
    def _save_email(self) -> None:
        """Conserve localement l'e-mail facultatif (cle Unpaywall)."""
        self.settings.setValue("unpaywall_email", self.email.text().strip())

    @staticmethod
    def _access_text(hit) -> str:
        """Colonne « Journal / accès » (compatible BookHit et ArticleHit)."""
        note = str(getattr(hit, "note", "") or "")
        if isinstance(hit, sb.ArticleHit):
            if hit.pdf_urls:
                note = f"{note} — {len(hit.pdf_urls)} PDF libre(s)"
            elif hit.pmcid:
                note = f"{note} — texte intégral (PMC) disponible"
            elif hit.doi:
                note = f"{note} — PDF : vérification possible"
        return note

    @staticmethod
    def _can_download(hit) -> bool:
        if hit is None:
            return False
        if isinstance(hit, sb.ArticleHit):
            # toujours tentable : a defaut de PDF editeur, le texte integral
            # PMC sert de repli (voir download_article et fulltext_html)
            return bool(hit.pdf_urls or hit.doi or hit.pmid or hit.pmcid)
        return bool(getattr(hit, "download_url", ""))

    @Slot(object)
    def _on_results(self, hits) -> None:
        self.button.setEnabled(True)
        self._hits = list(hits)
        self.table.setRowCount(0)
        for hit in self._hits:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                hit.source, hit.title, hit.authors, hit.year,
                self._access_text(hit),
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
        self.status.setText(
            f"{len(self._hits)} résultat(s). "
            "Double-clic : téléchargement du PDF si l'accès est libre, "
            "sinon ouverture de la page."
            if self._hits
            else "Aucun résultat pour cette recherche."
        )
        if self._hits:
            self.table.selectRow(0)
        self._update_buttons()

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.button.setEnabled(True)
        self.status.setText(f"Échec de la recherche : {message}")

    # -- actions ---------------------------------------------------------
    def _current_hit(self):
        row = self.table.currentRow()
        if 0 <= row < len(self._hits):
            return self._hits[row]
        return None

    def _update_buttons(self) -> None:
        hit = self._current_hit()
        self.open_button.setEnabled(bool(hit and getattr(hit, "url", "")))
        self.download_button.setEnabled(self._can_download(hit))

    def open_page(self) -> None:
        hit = self._current_hit()
        if hit and getattr(hit, "url", ""):
            QDesktopServices.openUrl(QUrl(hit.url))

    def _double_click(self) -> None:
        """PDF libre si possible, sinon la page de la reference."""
        hit = self._current_hit()
        if hit is None:
            return
        if self._can_download(hit):
            self.start_download()
        else:
            self.open_page()

    def start_download(self) -> None:
        hit = self._current_hit()
        if hit is None or not self._can_download(hit):
            return
        self.download_button.setEnabled(False)
        self.status.setText(f"Téléchargement : {hit.title}…")
        directory = self.directory
        root = self.root
        email = self.email.text().strip()

        def _job(progress, fraction):
            if isinstance(hit, sb.ArticleHit):
                def fallback():
                    """PDF editeurs bloques -> texte integral Europe PMC
                    converti en PDF localement (acces libre, sans dependance)."""
                    html = sb.fulltext_html(hit, progress=progress)
                    name = sb.slugify(hit.title or "article",
                                      fallback="article")
                    dest = sb.unique_path(directory / f"{name}.pdf")
                    progress("Écriture du PDF à partir du texte intégral …")
                    return render_html_pdf(html, dest, title=hit.title)

                return sb.download_article(
                    root, hit, directory,
                    filename=f"{hit.title or 'article'}.pdf",
                    progress=progress, fraction=fraction,
                    fallback=fallback, email=email,
                )
            return sb.download_file(
                hit.download_url, directory,
                filename=f"{hit.title or 'document'}.pdf",
                fraction=fraction, root=root,
            )

        worker = Worker(_job)
        self._workers.append(worker)
        worker.signals.finished.connect(self._on_downloaded)
        worker.signals.failed.connect(self._on_download_failed)
        self.pool.start(worker)

    @Slot(object)
    def _on_downloaded(self, path: Path) -> None:
        self.downloaded = Path(path)
        self.accept()

    @Slot(str)
    def _on_download_failed(self, message: str) -> None:
        self.download_button.setEnabled(True)
        self.status.setText(
            f"Échec du téléchargement : {message}\n"
            "Utilisez « Ouvrir la page » pour accéder à la référence."
        )


# --- volet gauche : explorateur ----------------------------------------------
class WorkspaceFilterModel(QSortFilterProxyModel):
    """N'affiche que la racine du repertoire de travail et ses descendants.

    QFileSystemModel sait lire tout le disque : on lui laisse donc passer la
    racine (et ses ancetres, necessaires pour raciner la vue dessus) tout en
    masquant les dossiers voisins. C'est ce qui permet d'afficher le repertoire
    de travail lui-meme comme noeud de l'arborescence.
    """

    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self.set_root(root)

    def set_root(self, root: Path) -> None:
        self._root = os.path.realpath(str(root))
        self._prefix = self._root.rstrip(os.sep) + os.sep
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:  # noqa: N802
        model = self.sourceModel()
        if model is None:
            return False
        index = model.index(source_row, 0, source_parent)
        path = os.path.realpath(model.filePath(index))
        if path == self._root:
            return True
        # ancetre de la racine : la vue est racinee sur le parent direct
        if self._root.startswith(path.rstrip(os.sep) + os.sep):
            return True
        return path.startswith(self._prefix)


class WorkspacePane(QWidget):
    """Arborescence du repertoire de travail (racine comprise), avec le bouton « + »."""

    documentActivated = Signal(object)
    pathActivated = Signal(object)
    pathSelected = Signal(object)
    requestNewSubdir = Signal(object)
    requestWikipediaPdf = Signal(object)
    requestPsychonautWikiPdf = Signal(object)
    requestExistingPdf = Signal(object)
    requestLiterature = Signal(object)
    requestMolecule = Signal(object)
    requestAssociate = Signal(object)
    requestRefreshResume = Signal(object)
    requestDelete = Signal(object)
    requestOpenInFileManager = Signal(object)
    requestCompare = Signal(object)
    requestInteractions = Signal(object)

    # organisation de l'arborescence (combo de la barre d'outils)
    VIEWS = (
        ("Arborescence", "tree"),
        ("Classe chimique", "chemical"),
        ("Drug class / Tolérance croisée", "drug_x_tol"),
    )

    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self.root = Path(root)
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.grouped_model: QStandardItemModel | None = None
        self._drag_origin: QPointF | None = None  # glisser depuis l'arborescence

        self.model = QFileSystemModel(self)
        self.model.setFilter(
            QDir.AllEntries | QDir.NoDotAndDotDot
        )
        self.proxy = WorkspaceFilterModel(self.root, self)
        self.proxy.setSourceModel(self.model)

        self.tree = QTreeView(self)
        self.tree.setModel(self.proxy)
        self.tree.setHeaderHidden(False)
        self.tree.setAnimated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        for column in (1, 2, 3):
            self.tree.setColumnHidden(column, True)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.doubleClicked.connect(self._double_clicked)
        # selection a la souris : le resume de la molecule pointee se met a
        # jour sans avoir a ouvrir le fichier (signal `clicked`, qui survit aux
        # changements de modele des vues classees).
        self.tree.clicked.connect(self._clicked)
        self.tree.setRootIsDecorated(True)
        # glisser vers le tableau de comparaison : le glisser natif de
        # QTreeView n'est pas fiable selon les modeles, on le declenche
        # nous-memes apres la distance habituelle.
        self.tree.viewport().installEventFilter(self)
        # touche Suppr sur l'arborescence : place l'element selectionne dans
        # la corbeille du dossier de travail (la confirmation est demandee
        # par la fenetre principale, via requestDelete).
        self.tree.installEventFilter(self)

        bar = QToolBar(self)
        bar.setIconSize(QSize(16, 16))
        self.add_button = QToolButton(self)
        self.add_button.setText("+")
        self.add_button.setToolTip(
            "Ajouter dans le dossier sélectionné : molécule, page Wikipédia "
            "ou PsychonautWiki en PDF, document existant, référence "
            "bibliographique (PubMed…) — agit aussi sur la racine."
        )
        self.add_button.setPopupMode(QToolButton.InstantPopup)
        self.add_button.setMenu(self._build_add_menu())
        bar.addWidget(self.add_button)

        self.new_dir_action = QAction("Nouveau sous-dossier…", self)
        self.new_dir_action.triggered.connect(
            lambda: self.requestNewSubdir.emit(self.current_dir())
        )
        bar.addAction(self.new_dir_action)

        refresh = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload),
            "Rafraîchir", self
        )
        refresh.triggered.connect(self.reload)
        bar.addAction(refresh)

        bar.addSeparator()
        view_row = QHBoxLayout()
        view_row.setContentsMargins(4, 0, 4, 0)
        view_row.addWidget(QLabel("Vue :", self))
        self.view_combo = QComboBox(self)
        for label, key in self.VIEWS:
            self.view_combo.addItem(label, key)
        self.view_combo.setToolTip(
            "Organisation de l'arborescence : dossier par dossier, ou "
            "molécules regroupées par classe chimique, ou par « drug class » / "
            "« tolérance croisée » (lecture des resume.json)."
        )
        saved = str(self.settings.value("workspace_view", "tree"))
        # compatibles avec les noms de modes precedents
        saved = {"drug_class": "drug_x_tol", "cross_tolerance": "drug_x_tol",
                 "effect": "drug_x_tol"}.get(saved, saved)
        self.view_combo.setCurrentIndex(max(0, self.view_combo.findData(saved)))
        self.view_combo.currentIndexChanged.connect(self._view_changed)
        view_row.addWidget(self.view_combo, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(bar)
        layout.addLayout(view_row)
        layout.addWidget(self.tree, 1)

        # largeur minimale : le choix de vue (ligne dediee) doit toujours
        # rester visible, meme si l'etat sauvegarde du splitter est etroit
        self.setMinimumWidth(320)

        self.set_root(self.root)

    # -- racine ----------------------------------------------------------
    def set_root(self, root: Path) -> None:
        """Definit la racine, affichee comme noeud de l'arborescence."""
        self.root = Path(root)
        if self.view_mode() != "tree":
            self._rebuild_grouped()
            return
        parent = self.root.parent
        self.model.setRootPath(str(parent))
        self.proxy.set_root(self.root)
        mapped_parent = self.proxy.mapFromSource(self.model.index(str(parent)))
        if mapped_parent.isValid():
            self.tree.setRootIndex(mapped_parent)
        mapped_root = self.proxy.mapFromSource(self.model.index(str(self.root)))
        if mapped_root.isValid():
            self.tree.expand(mapped_root)

    def reload(self) -> None:
        self.set_root(self.root)

    # -- vues classees (classe chimique / effet) -------------------------
    def view_mode(self) -> str:
        return str(self.view_combo.currentData())

    @Slot()
    def _view_changed(self) -> None:
        self.settings.setValue("workspace_view", self.view_mode())
        self._apply_view()

    def _apply_view(self) -> None:
        if self.view_mode() == "tree":
            self.grouped_model = None
            self.tree.setHeaderHidden(False)
            self.tree.setSortingEnabled(False)  # fs model gère le tri
            self.tree.setModel(self.proxy)
            self.set_root(self.root)
        else:
            self.tree.setSortingEnabled(False)
            self._rebuild_grouped()

    def _rebuild_grouped(self) -> None:
        """Vue « classe chimique » / « drug class / tolérance croisée ».

        On conserve une arborescence : des dossiers virtuels (les groupes)
        parent les dossiers reels des molecules. Les libelles qui ne different
        que par la casse ou le pluriel (« Dissociatives », « dissociative »)
        sont fusionnes en un seul groupe via une cle normalisee ; le libelle
        affiche est celui en majuscule (la forme officielle PsychonautWiki).
        """
        catalogue = sb.molecule_catalogue(self.root)

        if self.view_mode() == "chemical":
            field, fallback = "chemical_class", "Sans classe chimique"
        else:  # drug_x_tol : drug class ET tolerance croisee confondues
            field, fallback = None, "Sans drug class / tolérance croisée"

        def sort_key(text: str):
            folded = rm.fold_accents(text).casefold()
            return (folded, text)

        def norm_key(text: str) -> str:
            """Cle de groupe : sans accents, minuscules, au singulier."""
            folded = rm.fold_accents(text).casefold().strip()
            if len(folded) > 3 and folded.endswith("ies"):
                folded = folded[:-3] + "y"
            elif len(folded) > 3 and folded.endswith("s"):
                folded = folded[:-1]
            return folded

        def label_score(label: str):
            """Prefere la forme officielle : initiale en majuscule, pluriel."""
            return (label[:1].isupper(), len(label))

        # regroupement fusionne (une cle = un dossier virtuel)
        slots: dict[str, dict] = {}
        for entry in catalogue:
            if field:
                values = list(entry.get(field) or [])
            else:
                values = []
                for source in ("drug_class", "cross_tolerance"):
                    for value in (entry.get(source) or []):
                        if value not in values:
                            values.append(value)
            if not values:
                values = [fallback]
            for value in values:
                key = norm_key(value)
                slot = slots.get(key)
                if slot is None:
                    slots[key] = {"label": value, "members": [entry]}
                    continue
                if label_score(value) > label_score(slot["label"]):
                    slot["label"] = value
                # une meme molecule ne figure qu'une fois par groupe
                if all(m["path"] != entry["path"] for m in slot["members"]):
                    slot["members"].append(entry)

        model = QStandardItemModel(self)
        model.setHorizontalHeaderLabels(["Arborescence"])
        root_item = model.invisibleRootItem()
        names = sorted(
            slots,
            key=lambda k: (slots[k]["label"].startswith("Sans "),
                           sort_key(slots[k]["label"])),
        )
        for key in names:
            slot = slots[key]
            members = sorted(slot["members"],
                             key=lambda entry: sort_key(entry["name"]))
            group = QStandardItem(slot["label"])
            group.setEditable(False)
            group.setData(None, Qt.UserRole)
            group.setToolTip(f"{len(members)} molécule(s)")
            for entry in members:
                item = QStandardItem(entry["name"])
                item.setEditable(False)
                details = [entry["path"],
                           "Double-clic : ouvrir le résumé"]
                if entry.get("chemical_class"):
                    details.append(
                        "Classe : " + ", ".join(entry["chemical_class"])
                    )
                if entry.get("drug_class"):
                    details.append(
                        "Drug class : " + ", ".join(entry["drug_class"])
                    )
                if entry.get("cross_tolerance"):
                    details.append(
                        "Tolérance croisée : " + ", ".join(entry["cross_tolerance"])
                    )
                if entry.get("effects"):
                    details.append(
                        f"{len(entry['effects'])} effet(s) rapporté(s)"
                    )
                item.setToolTip("\n".join(details))
                item.setData(entry["path"], Qt.UserRole)
                group.appendRow(item)
            root_item.appendRow(group)

        self.grouped_model = model
        self.tree.setModel(model)
        self.tree.setHeaderHidden(True)
        if len(names) <= 50:
            self.tree.expandAll()
        else:
            self.tree.collapseAll()

    # -- selection -------------------------------------------------------
    def _path_of(self, proxy_index) -> Path | None:
        """Chemin derriere un index de la vue (arborescence ou vue classee)."""
        if proxy_index is None or not proxy_index.isValid():
            return None
        if (self.grouped_model is not None
                and self.tree.model() is self.grouped_model):
            item = self.grouped_model.itemFromIndex(proxy_index)
            value = item.data(Qt.UserRole) if item is not None else None
            return Path(str(value)) if value else None
        source = self.proxy.mapToSource(proxy_index)
        if not source.isValid():
            return None
        return Path(self.model.filePath(source))

    def current_dir(self) -> Path:
        """Dossier cible des actions : le dossier selectionne, sinon la racine."""
        path = self._path_of(self.tree.currentIndex())
        if path is None:
            return self.root
        return path if path.is_dir() else path.parent

    def current_path(self) -> Path | None:
        return self._path_of(self.tree.currentIndex())

    # -- glisser vers le tableau de comparaison --------------------------
    @staticmethod
    def _path_mime(path: Path) -> QMimeData:
        """MIME transporte par un glisser de l'explorateur (chemin cible)."""
        mime = QMimeData()
        mime.setData(PATH_MIME, str(path).encode("utf-8"))
        mime.setText(str(path))
        return mime

    def eventFilter(self, obj, event):  # noqa: N802 - API Qt
        """Glisser (PATH_MIME) apres la distance standard, et touche Suppr.

        Suppr sur l'arborescence place l'element selectionne dans
        `.corbeille` (via `requestDelete` ; la confirmation est posee par la
        fenetre principale). La racine du repertoire de travail est protegee.
        """
        if obj is self.tree and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Delete and not event.modifiers():
                path = self.current_path()
                if path is not None and path != self.root:
                    self.requestDelete.emit(path)
                    return True
        if obj is self.tree.viewport():
            kind = event.type()
            if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag_origin = event.position()
            elif kind == QEvent.MouseMove and self._drag_origin is not None:
                if event.buttons() & Qt.LeftButton:
                    delta = event.position() - self._drag_origin
                    if (abs(delta.x()) + abs(delta.y())
                            >= QApplication.startDragDistance()):
                        self._drag_origin = None
                        self._start_path_drag()
                        return True
            elif kind in (QEvent.MouseButtonRelease, QEvent.Leave):
                self._drag_origin = None
        return super().eventFilter(obj, event)

    def _start_path_drag(self) -> None:
        """Glisse la molecule selectionnee (ou son dossier) vers le tableau."""
        path = self._path_of(self.tree.currentIndex())
        if path is None:
            return
        drag = QDrag(self.tree)
        drag.setMimeData(self._path_mime(path))
        drag.exec(Qt.CopyAction)

    # -- menus -----------------------------------------------------------
    def _build_add_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setTitle("Ajouter…")
        menu.addAction("Créer un sous-dossier…", lambda: self.requestNewSubdir.emit(
            self.current_dir()
        ))
        menu.addSeparator()
        menu.addAction("Ajouter une molécule…", lambda: self.requestMolecule.emit(
            self.current_dir()
        ))
        menu.addAction(
            "Ajouter le PDF d'une page Wikipédia…",
            lambda: self.requestWikipediaPdf.emit(self.current_dir()),
        )
        menu.addAction(
            "Ajouter le PDF d'une page PsychonautWiki…",
            lambda: self.requestPsychonautWikiPdf.emit(self.current_dir()),
        )
        menu.addAction("Ajouter un document existant…", lambda: self.requestExistingPdf.emit(
            self.current_dir()
        ))
        menu.addAction(
            "Rechercher un article ou un livre…",
            lambda: self.requestLiterature.emit(self.current_dir()),
        )
        return menu

    def _context_menu(self, point) -> None:
        index = self.tree.indexAt(point)
        menu = QMenu(self)
        path = self._path_of(index)
        if path is None:
            menu.addAction("Créer un sous-dossier…", lambda: self.requestNewSubdir.emit(
                self.root
            ))
            menu.addSeparator()
            menu.addMenu(self._build_add_menu())
            menu.exec(self.tree.viewport().mapToGlobal(point))
            return

        is_dir = path.is_dir()

        if is_dir:
            menu.addAction("Nouveau sous-dossier…", lambda: self.requestNewSubdir.emit(
                path
            ))
            menu.addMenu(self._build_add_menu())
            menu.addSeparator()
            menu.addAction(
                "Associer à une molécule…",
                lambda: self.requestAssociate.emit(path),
            )
            menu.addAction(
                "Régénérer le résumé",
                lambda: self.requestRefreshResume.emit(path),
            )
            menu.addSeparator()
            menu.addAction(
                "Ajouter à la comparaison",
                lambda: self.requestCompare.emit(path),
            )
            menu.addAction(
                "Ajouter aux interactions",
                lambda: self.requestInteractions.emit(path),
            )
        else:
            if sb.document_kind(path) != "other":
                menu.addAction("Ouvrir dans le lecteur", lambda: self.requestOpen(path))
            menu.addAction(
                "Ouvrir avec l'application par défaut",
                lambda: self.pathActivated.emit(path),
            )
            menu.addSeparator()
            menu.addAction(
                "Associer ce dossier à une molécule…",
                lambda: self.requestAssociate.emit(path.parent),
            )
            menu.addAction(
                "Ajouter à la comparaison",
                lambda: self.requestCompare.emit(path),
            )
            menu.addAction(
                "Ajouter aux interactions",
                lambda: self.requestInteractions.emit(path),
            )

        menu.addSeparator()
        menu.addAction(
            "Afficher dans le gestionnaire de fichiers",
            lambda: self.requestOpenInFileManager.emit(path),
        )
        menu.addAction(
            "Placer dans la corbeille",
            lambda: self.requestDelete.emit(path),
        )
        menu.exec(self.tree.viewport().mapToGlobal(point))

    def requestOpen(self, path: Path) -> None:
        """Ouvre un document : dans le lecteur si Qt sait l'afficher, sinon
        avec l'application du systeme."""
        path = Path(path)
        if sb.document_kind(path) == "other":
            self.pathActivated.emit(path)
        else:
            self.documentActivated.emit(path)

    def _clicked(self, index) -> None:
        """Signale la simple selection (aperçu du resume, sans ouvrir)."""
        path = self._path_of(index)
        if path is not None:
            self.pathSelected.emit(path)

    def _double_clicked(self, index) -> None:
        path = self._path_of(index)
        if path is None:
            return
        if path.is_dir():
            # vue classee : double-clic sur une molecule -> son resume
            if (self.grouped_model is not None
                    and self.tree.model() is self.grouped_model):
                resume = sb.resume_html_path(path)
                if resume.is_file():
                    self.documentActivated.emit(resume)
            return
        self.requestOpen(path)


# --- volet central : lecteur PDF ---------------------------------------------
class DocumentPane(QWidget):
    """Base des onglets document : tous exposent `.path`."""

    kind = "document"

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = Path(path)

    def focus_search(self) -> None:
        """Place le curseur dans le champ de recherche (Ctrl+F)."""


class PdfPane(DocumentPane):
    """Lecteur d'un PDF : navigation, zoom, recherche dans le texte."""

    kind = "pdf"

    def __init__(self, path: Path, parent=None):
        super().__init__(path, parent)

        self.document = QPdfDocument(self)
        self.document.load(str(self.path))
        self.search_model = QPdfSearchModel(self)
        self.search_model.setDocument(self.document)

        self.view = QPdfView(self)
        self.view.setDocument(self.document)
        self.view.setPageMode(QPdfView.PageMode.MultiPage)
        self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.view.setSearchModel(self.search_model)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(350)
        self._search_timer.timeout.connect(self._run_search)
        self.search_model.countChanged.connect(self._update_search_label)

        bar = QToolBar(self)
        bar.setIconSize(QSize(16, 16))

        self.prev_action = QAction("◀", self)
        self.prev_action.setToolTip("Page précédente")
        self.prev_action.triggered.connect(lambda: self._step_page(-1))
        bar.addAction(self.prev_action)

        self.page_label = QLabel(self)
        self.page_label.setMinimumWidth(90)
        self.page_label.setAlignment(Qt.AlignCenter)
        bar.addWidget(self.page_label)

        self.next_action = QAction("▶", self)
        self.next_action.setToolTip("Page suivante")
        self.next_action.triggered.connect(lambda: self._step_page(1))
        bar.addAction(self.next_action)

        bar.addSeparator()

        self.zoom_combo = QComboBox(self)
        self.zoom_combo.addItems(
            ["Largeur", "Page entière", "50 %", "75 %", "100 %", "125 %",
             "150 %", "200 %", "400 %"]
        )
        self.zoom_combo.currentIndexChanged.connect(self._apply_zoom)
        bar.addWidget(self.zoom_combo)

        bar.addSeparator()

        self.search_field = QLineEdit(self)
        self.search_field.setPlaceholderText("Rechercher dans le PDF…")
        self.search_field.setClearButtonEnabled(True)
        self.search_field.setMaximumWidth(260)
        self.search_field.textChanged.connect(lambda _t: self._search_timer.start())
        self.search_field.returnPressed.connect(self._run_search)
        bar.addWidget(self.search_field)

        self.search_prev = QAction("▲", self)
        self.search_prev.setToolTip("Résultat précédent")
        self.search_prev.triggered.connect(lambda: self._step_result(-1))
        bar.addAction(self.search_prev)

        self.search_next = QAction("▼", self)
        self.search_next.setToolTip("Résultat suivant")
        self.search_next.triggered.connect(lambda: self._step_result(1))
        bar.addAction(self.search_next)

        self.search_label = QLabel(self)
        self.search_label.setMinimumWidth(110)
        bar.addWidget(self.search_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(bar)
        layout.addWidget(self.view, 1)

        nav = self.view.pageNavigator()
        nav.currentPageChanged.connect(self._sync_page_label)
        self.document.pageCountChanged.connect(self._sync_page_label)
        self._sync_page_label()

    # -- navigation ------------------------------------------------------
    def page_count(self) -> int:
        return self.document.pageCount()

    def _jump_to(self, page: int) -> None:
        """Saute a une page (l'API de navigation de Qt n'accepte pas un page
        seule : on fournit une position nulle)."""
        total = self.page_count()
        if total <= 0:
            return
        page = min(max(page, 0), total - 1)
        self.view.pageNavigator().jump(page, QPointF(0.0, 0.0))

    def _sync_page_label(self, *_args) -> None:
        nav = self.view.pageNavigator()
        total = max(self.page_count(), 1)
        self.page_label.setText(f"Page {nav.currentPage() + 1} / {total}")

    def _step_page(self, delta: int) -> None:
        nav = self.view.pageNavigator()
        total = self.page_count()
        if total <= 0:
            return
        self._jump_to(min(max(nav.currentPage() + delta, 0), total - 1))

    def focus_search(self) -> None:
        self.search_field.setFocus()
        self.search_field.selectAll()

    # -- zoom ------------------------------------------------------------
    def _apply_zoom(self) -> None:
        choice = self.zoom_combo.currentText()
        if choice == "Largeur":
            self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        elif choice == "Page entière":
            self.view.setZoomMode(QPdfView.ZoomMode.FitInView)
        else:
            factor = int(choice.replace(" %", "")) / 100.0
            self.view.setZoomMode(QPdfView.ZoomMode.Custom)
            self.view.setZoomFactor(factor)

    # -- recherche -------------------------------------------------------
    def _run_search(self) -> None:
        term = self.search_field.text().strip()
        self.search_model.setSearchString(term)
        self._update_search_label()

    def _update_search_label(self, *_args) -> None:
        if not self.search_field.text().strip():
            self.search_label.setText("")
            return
        count = self.search_model.count()
        self.search_label.setText(
            f"{count} résultat(s)" if count else "aucun résultat"
        )

    def _step_result(self, delta: int) -> None:
        count = self.search_model.count()
        if count <= 0:
            return
        current = self.view.currentSearchResultIndex()
        index = (current + delta) % count if current >= 0 else (
            0 if delta >= 0 else count - 1
        )
        self.view.setCurrentSearchResultIndex(index)
        result = self.search_model.resultAtIndex(index)
        if result is not None:
            self._jump_to(result.page())


# --- volet central : documents hors PDF ---------------------------------------
def render_html_pdf(html: str, dest: Path, title: str = "") -> Path:
    """Convertit un document HTML en PDF avec Qt (aucun acces reseau).

    Utilise pour fabriquer le PDF des pages PsychonautWiki. Le `QPdfWriter`
    est detruit a la sortie de la fonction : le fichier est donc ferme avant
    d'etre ouvert par l'application.
    """
    dest = Path(dest)
    document = QTextDocument()
    document.setHtml(html)
    writer = QPdfWriter(str(dest))
    writer.setPageSize(QPageSize(QPageSize.A4))
    writer.setPageMargins(
        QMarginsF(14.0, 14.0, 14.0, 14.0), QPageLayout.Millimeter
    )
    writer.setResolution(96)
    if title:
        writer.setTitle(title)
    document.print_(writer)
    return dest


class SearchableTextPane(DocumentPane):
    """Barre de recherche commune aux volets texte."""

    def _build_find_bar(self, find_callable, placeholder: str) -> QToolBar:
        bar = QToolBar(self)
        bar.setIconSize(QSize(16, 16))
        self.search_field = QLineEdit(bar)
        self.search_field.setPlaceholderText(placeholder)
        self.search_field.setClearButtonEnabled(True)
        self.search_field.setMaximumWidth(280)
        self.search_field.textChanged.connect(lambda _t: find_callable(1))
        self.search_field.returnPressed.connect(lambda: find_callable(1))
        bar.addWidget(self.search_field)

        prev = QAction("▲", bar)
        prev.setToolTip("Résultat précédent")
        prev.triggered.connect(lambda: find_callable(-1))
        bar.addAction(prev)
        nxt = QAction("▼", bar)
        nxt.setToolTip("Résultat suivant")
        nxt.triggered.connect(lambda: find_callable(1))
        bar.addAction(nxt)

        self.search_label = QLabel(bar)
        self.search_label.setMinimumWidth(110)
        bar.addWidget(self.search_label)
        return bar

    def focus_search(self) -> None:
        self.search_field.setFocus()
        self.search_field.selectAll()


class TextPane(SearchableTextPane):
    """Fichier texte (notes, CSV, JSON, code…) : lecture seule et recherche."""

    kind = "text"

    def __init__(self, path: Path, parent=None):
        super().__init__(path, parent)
        self.edit = QPlainTextEdit(self)
        self.edit.setReadOnly(True)
        self.edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = self.edit.font()
        font.setFamily("monospace")
        self.edit.setFont(font)
        content = sb.read_text_document(self.path)
        self.edit.setPlainText(content if content else "(fichier vide ou illisible)")
        bar = self._build_find_bar(self._find, "Rechercher dans le texte…")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(bar)
        layout.addWidget(self.edit, 1)

    def _find(self, delta: int) -> None:
        term = self.search_field.text()
        if not term:
            self.search_label.setText("")
            return
        flags = QTextDocument.FindFlag(0)
        if delta < 0:
            flags |= QTextDocument.FindFlag.FindBackward
        found = self.edit.find(term, flags)
        if not found:
            # boucle : on repart du debut (ou de la fin si l'on remonte)
            cursor = self.edit.textCursor()
            cursor.movePosition(
                QTextCursor.MoveOperation.Start if delta > 0
                else QTextCursor.MoveOperation.End
            )
            self.edit.setTextCursor(cursor)
            found = self.edit.find(term, flags)
        self.search_label.setText("" if found else "aucun résultat")


class HtmlPane(SearchableTextPane):
    """Page HTML locale ou document EPUB (deja converti en HTML autonome)."""

    kind = "html"

    def __init__(self, path: Path, html: str | None = None, parent=None):
        super().__init__(path, parent)
        self.browser = QTextBrowser(self)
        self.browser.setOpenExternalLinks(True)
        if html is None and path.suffix.lower() in sb.HTML_SUFFIXES:
            # ressources locales (images, CSS) resolues par rapport au fichier
            self.browser.setSource(QUrl.fromLocalFile(str(self.path)))
        else:
            self.browser.setHtml(html or "")
        bar = self._build_find_bar(self._find, "Rechercher dans la page…")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(bar)
        layout.addWidget(self.browser, 1)

    def _find(self, delta: int) -> None:
        term = self.search_field.text()
        if not term:
            self.search_label.setText("")
            return
        flags = QTextDocument.FindFlag(0)
        if delta < 0:
            flags |= QTextDocument.FindFlag.FindBackward
        found = self.browser.find(term, flags)
        self.search_label.setText("" if found else "aucun résultat")


class ImagePane(DocumentPane):
    """Image (PNG, JPEG, GIF…) : affichage avec barres de defilement."""

    kind = "image"

    def __init__(self, path: Path, parent=None):
        super().__init__(path, parent)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        pixmap = QPixmap(str(self.path))
        if pixmap.isNull():
            self.label.setText(f"Image illisible : {self.path.name}")
        else:
            self.label.setPixmap(pixmap)

        scroll = QScrollArea(self)
        scroll.setWidget(self.label)
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(scroll, 1)


# --- volet droit : resume de la molecule -------------------------------------
class SummaryPane(QWidget):
    """Tableau HTML du parser pour la molecule du document ouvert."""

    refreshRequested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.molecule = ""
        self.molecule_dir: Path | None = None

        bar = QToolBar(self)
        bar.setIconSize(QSize(16, 16))
        self.title = QLabel("Aucune molécule sélectionnée", self)
        self.title.setStyleSheet("font-weight: bold; padding: 0 8px;")
        bar.addWidget(self.title)

        spacer = QWidget(self)
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(spacer)

        self.refresh_action = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload),
            "Régénérer le résumé", self
        )
        self.refresh_action.setToolTip(
            "Relance le parser sur les sources en ligne et réécrit "
            "resume.html / resume.json"
        )
        self.refresh_action.triggered.connect(
            lambda: self.refreshRequested.emit(self.molecule_dir)
        )
        self.refresh_action.setEnabled(False)
        bar.addAction(self.refresh_action)

        self.browser = QTextBrowser(self)
        self.browser.setOpenExternalLinks(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(bar)
        layout.addWidget(self.browser, 1)

        self.show_message(
            "Aucune molécule sélectionnée",
            "Ouvrez un document : le résumé de la molécule de son dossier "
            "s'affichera ici. Pour créer un dossier complet (PDF Wikipédia + "
            "PDF PsychonautWiki + résumé), utilisez le bouton « + » de "
            "l'explorateur.",
        )

    def show_message(self, title: str, text: str) -> None:
        self.title.setText(title)
        self.molecule = ""
        self.molecule_dir = None
        self.refresh_action.setEnabled(False)
        self.browser.setHtml(
            f"<h3>{sb.escape_html(title)}</h3><p style='color:#555'>{sb.escape_html(text)}</p>"
        )

    def show_molecule(self, molecule_dir: Path, molecule: str) -> None:
        """Affiche le resume pre-rendu de `molecule_dir` (aucun acces reseau).

        Le titre affiche le nom **declare dans le resume** quand il differe
        du nom de dossier : le contenu et l'etiquette ne peuvent alors plus
        se contredire (dossier renomme ou mal associe). L'infobulle donne le
        chemin complet du dossier source.
        """
        molecule_dir = Path(molecule_dir)
        declared = sb.resume_molecule_name(molecule_dir)
        self.molecule = declared or molecule
        self.molecule_dir = molecule_dir
        label = self.molecule or molecule_dir.name
        folder = molecule_dir.name
        if folder and self.molecule and rm.fold_accents(folder).casefold() \
                != rm.fold_accents(self.molecule).casefold():
            label = f"{self.molecule} — dossier « {folder} »"
        self.title.setText(label)
        self.title.setToolTip(str(molecule_dir))
        self.refresh_action.setEnabled(True)

        html = sb.read_text_or_empty(sb.resume_html_path(molecule_dir))
        if html:
            self.browser.setHtml(html)
            return

        markdown = sb.read_text_or_empty(sb.resume_markdown_path(molecule_dir))
        if markdown:
            self.browser.setMarkdown(markdown)
            return

        # resume.html absent mais resume.json present : on reaffiche le resume
        # deja recupere, reconstruit localement et sans reseau.
        rebuilt = sb.ensure_resume_html(molecule_dir)
        if rebuilt:
            self.browser.setHtml(rebuilt)
            return

        self.browser.setHtml(
            "<h3>"
            + sb.escape_html(molecule or molecule_dir.name)
            + "</h3><p style='color:#555'>Aucun résumé pour l'instant. "
            "Cliquez sur « Régénérer le résumé » pour interroger "
            "PsychonautWiki et Wikipédia.</p>"
        )

    def clear(self) -> None:
        self.show_message(
            "Aucune molécule sélectionnée",
            "Ouvrez un PDF dont le dossier correspond à une molécule.",
        )

    def show_loading(self, molecule: str) -> None:
        """Message d'attente pendant la recuperation automatique du resume.

        Contrairement a `show_message`, le dossier de molecule reste memorise :
        le resultat de la tache de fond peut donc s'afficher a la fin.
        """
        self.browser.setHtml(
            "<h3>"
            + sb.escape_html(molecule)
            + "</h3><p style='color:#555'>Récupération du résumé en cours…</p>"
        )


# --- accueil ------------------------------------------------------------------
class WelcomePane(QWidget):
    """Ecran d'accueil quand le repertoire de travail est introuvable."""

    chooseRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        title = QLabel("Atelier de recherche psychopharmacologique", self)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        layout.addWidget(title)
        text = QLabel(
            "Choisissez un répertoire de travail : il contiendra un dossier par\n"
            "molécule (PDF Wikipédia + résumé) et vos documents.\n\n" + DISCLAIMER,
            self,
        )
        text.setAlignment(Qt.AlignCenter)
        text.setStyleSheet("color: #555;")
        layout.addWidget(text)
        button = QPushButton("Choisir un répertoire de travail…", self)
        button.clicked.connect(self.chooseRequested.emit)
        layout.addWidget(button, alignment=Qt.AlignCenter)
        layout.addStretch(1)


# --- page comparaison : tableau des molecules --------------------------------
class CompareTable(QTableWidget):
    """Tableau de comparaison : interactions au clavier et en glisser-deposer.

    * touche « Suppr » -> `deleteRequested` (supprime les lignes selectionnees) ;
    * depot d'un chemin de l'explorateur (PATH_MIME) -> `pathDropped` ;
    * glisser des lignes du tableau (ROW_MIME) -> `rowMoveRequested`,
      signale au ComparePane qui recalcule le nouvel ordre.

    Seuls ces deux types MIME sont acceptes : un depot venant d'ailleurs
    (gestionnaire de fichiers par exemple) est ignore.
    """

    ROW_MIME = "application/x-pharma-rows"

    deleteRequested = Signal()
    pathDropped = Signal(object)
    rowMoveRequested = Signal(object, float)

    def __init__(self, parent=None):
        super().__init__(0, 0, parent)
        # un glisser reel arrive sur le viewport, que QAbstractScrollArea
        # relaie vers ces gestionnaires : les deux doivent accepter.
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)

    # -- glisser-deposer --------------------------------------------------
    @staticmethod
    def _has_our_mime(mime: QMimeData) -> bool:
        return mime.hasFormat(PATH_MIME) or mime.hasFormat(
            CompareTable.ROW_MIME
        )

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - API Qt
        if self._has_our_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - API Qt
        if self._has_our_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802 - API Qt
        mime = event.mimeData()
        if mime.hasFormat(self.ROW_MIME):
            try:
                text = bytes(mime.data(self.ROW_MIME)).decode("utf-8")
                rows = [int(x) for x in text.split(",") if x.strip()]
            except ValueError:
                event.ignore()
                return
            self.rowMoveRequested.emit(rows, float(event.position().y()))
            event.acceptProposedAction()
        elif mime.hasFormat(PATH_MIME):
            try:
                path = bytes(mime.data(PATH_MIME)).decode("utf-8").strip()
            except UnicodeDecodeError:
                event.ignore()
                return
            if path:
                self.pathDropped.emit(Path(path))
                event.acceptProposedAction()
            else:
                event.ignore()
        else:
            event.ignore()

    def _dragged_rows(self) -> list[int]:
        """Indices de `self.rows` correspondant aux lignes selectionnees."""
        idxs: list[int] = []
        for index in self.selectedIndexes():
            if index.column() != 0:
                continue
            item = self.item(index.row(), 0)
            stored = item.data(Qt.UserRole + 1) if item is not None else None
            if stored is not None and int(stored) not in idxs:
                idxs.append(int(stored))
        return idxs

    def startDrag(self, actions) -> None:  # noqa: N802 - API Qt
        """Departe un glisser de lignes : transporte les indices de liste."""
        idxs = self._dragged_rows()
        if not idxs:
            return
        mime = QMimeData()
        mime.setData(
            self.ROW_MIME, ",".join(str(i) for i in idxs).encode("utf-8")
        )
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.MoveAction)

    # -- clavier ----------------------------------------------------------
    def keyPressEvent(self, event) -> None:  # noqa: N802 - API Qt
        if event.key() == Qt.Key_Delete and self.rowCount():
            self.deleteRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


# --- volet tableau (page Comparaison) -----------------------------------------
class ComparePane(QWidget):
    """Tableau de comparaison : une ligne par molecule (page « Comparaison »).

    Chaque ligne donne l'etat de la molecule sur le disque (disponible,
    absente, en file, telechargement…, echec) puis ses champs de
    `rm.TARGET_ORDER` lus dans resume.json (doublon, tooltip complet).
    Le tableau ne contient que des donnees locales : aucun acces reseau ici.
    """

    addRequested = Signal()
    removeRequested = Signal()
    clearRequested = Signal()
    downloadMissingRequested = Signal()
    refreshRequested = Signal()
    saveRequested = Signal(object)
    loadRequested = Signal(object)
    openRequested = Signal(object)
    pathDropped = Signal(object)   # chemin depose depuis l'explorateur
    rowsChanged = Signal()         # ordre des lignes modifie (a persister)

    STATE_COLORS = {
        "disponible": "#e3f2e3",
        "absente": "#f0f0f0",
        "en file": "#e1efff",
        "téléchargement…": "#cfe3ff",
        "échec": "#fbe3e3",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.rows: list[dict] = []
        # colonnes : (libelle, cle) — « Molécule » et « État » jamais masquees
        self.columns: list[tuple[str, str]] = [
            ("Molécule", "__name"),
            ("État", "__state"),
        ]
        for key in rm.TARGET_ORDER:
            if key != "name":
                self.columns.append((rm.LABELS_FR.get(key, key), key))
        try:
            self._hidden: set[str] = set(
                json.loads(str(self.settings.value("compare_columns", "[]")))
            )
        except ValueError:
            self._hidden = set()

        bar = QToolBar(self)
        bar.setIconSize(QSize(16, 16))
        bar.addAction("Ajouter une molécule…").triggered.connect(
            self.addRequested
        )
        download = QAction("Télécharger les absentes", self)
        download.setToolTip(
            "Met en file d'attente les molécules du tableau qui n'ont pas "
            "encore de dossier sur le disque."
        )
        download.triggered.connect(self.downloadMissingRequested)
        bar.addAction(download)
        bar.addAction("Retirer la sélection").triggered.connect(
            self.removeRequested
        )
        bar.addAction("Vider").triggered.connect(self.clearRequested)
        bar.addSeparator()
        save = QAction("Enregistrer…", self)
        save.setToolTip(
            "Enregistre les deux tableaux (comparaison et interactions) dans "
            "un fichier JSON."
        )
        save.triggered.connect(lambda: self.saveRequested.emit("both"))
        bar.addAction(save)
        save_c = QAction("Enregistrer ce tableau…", self)
        save_c.setToolTip("Enregistre uniquement ce tableau (comparaison).")
        save_c.triggered.connect(lambda: self.saveRequested.emit("comparaison"))
        bar.addAction(save_c)
        load = QAction("Charger…", self)
        load.setToolTip(
            "Recharge les deux tableaux depuis un fichier JSON enregistré."
        )
        load.triggered.connect(lambda: self.loadRequested.emit("both"))
        bar.addAction(load)
        load_c = QAction("Charger dans ce tableau…", self)
        load_c.setToolTip(
            "Recharge uniquement le tableau de comparaison depuis un fichier JSON."
        )
        load_c.triggered.connect(lambda: self.loadRequested.emit("comparaison"))
        bar.addAction(load_c)
        bar.addSeparator()
        columns_button = QToolButton(self)
        columns_button.setText("Colonnes")
        columns_button.setPopupMode(QToolButton.InstantPopup)
        columns_button.setMenu(self._build_columns_menu())
        bar.addWidget(columns_button)
        refresh = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload),
            "Actualiser", self,
        )
        refresh.setToolTip(
            "Relit les resume.json sur le disque (états et valeurs)."
        )
        refresh.triggered.connect(self.refreshRequested)
        bar.addAction(refresh)
        self.count_label = QLabel(self)
        self.count_label.setMinimumWidth(160)
        self.count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bar.addWidget(self.count_label)

        self.table = CompareTable(self)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.itemDoubleClicked.connect(self._double_clicked)
        self.table.deleteRequested.connect(self.removeRequested)
        self.table.pathDropped.connect(self.pathDropped)
        self.table.rowMoveRequested.connect(self._move_rows)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(bar)
        layout.addWidget(self.table, 1)

        self.show_rows([])

    # -- colonnes ---------------------------------------------------------
    def hidden_columns(self) -> list[str]:
        """Cles des colonnes masquees (pour l'enregistrement des tableaux)."""
        return sorted(self._hidden)

    def set_hidden_columns(self, columns) -> None:
        """Fixe les colonnes masquees (rechargement d'un fichier de tableaux)."""
        valid = {key for _, key in self.columns}
        self._hidden = {str(c) for c in (columns or []) if str(c) in valid}
        self.settings.setValue(
            "compare_columns", json.dumps(sorted(self._hidden))
        )
        self.show_rows(self.rows)

    def _build_columns_menu(self) -> QMenu:
        menu = QMenu(self)
        for label, key in self.columns:
            if key == "__name":
                continue  # la colonne « Molécule » reste toujours visible
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(key not in self._hidden)
            action.toggled.connect(
                lambda checked, k=key: self._toggle_column(k, checked)
            )
        return menu

    def _toggle_column(self, key: str, checked: bool) -> None:
        if checked:
            self._hidden.discard(key)
        else:
            self._hidden.add(key)
        self.settings.setValue(
            "compare_columns", json.dumps(sorted(self._hidden))
        )
        self.show_rows(self.rows)

    # -- tableau ----------------------------------------------------------
    def show_rows(self, rows: list[dict]) -> None:
        """(Re)construit le tableau. Ligne = name, dir, state, fields."""
        self.rows = [dict(row) for row in rows]
        visible = [
            (label, key) for label, key in self.columns
            if key not in self._hidden
        ]
        self.table.setSortingEnabled(False)
        self.table.setColumnCount(len(visible))
        self.table.setHorizontalHeaderLabels(
            [label for label, _ in visible]
        )
        self.table.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            fields = row.get("fields") or {}
            for c, (_, key) in enumerate(visible):
                if key == "__name":
                    value = str(row.get("name", ""))
                elif key == "__state":
                    value = str(row.get("state", ""))
                else:
                    value = str(fields.get(key, ""))
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                # indice dans self.rows : necessaire pour retrouver la
                # ligne reelle meme quand le tableau est trie
                item.setData(Qt.UserRole + 1, r)
                if key == "__name":
                    item.setData(Qt.UserRole, row.get("dir"))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                elif key == "__state":
                    color = self.STATE_COLORS.get(value)
                    if color:
                        item.setBackground(QBrush(QColor(color)))
                self.table.setItem(r, c, item)
        for c, (_, key) in enumerate(visible):
            if key == "__name":
                self.table.setColumnWidth(c, 160)
            elif key == "__state":
                self.table.setColumnWidth(c, 120)
            else:
                self.table.setColumnWidth(c, 180)
        self.table.setSortingEnabled(True)
        self._update_count()

    def _update_count(self) -> None:
        total = len(self.rows)
        if not total:
            self.count_label.setText("Aucune molécule")
            self.count_label.setToolTip(
                "Ajoutez des molécules avec « Ajouter une molécule… » ou "
                "par clic droit dans l'explorateur."
            )
            return
        available = sum(
            1 for row in self.rows if row.get("state") == "disponible"
        )
        self.count_label.setText(f"{total} molécule(s), {available} dispo.")
        self.count_label.setToolTip("")

    def _double_clicked(self, item: QTableWidgetItem) -> None:
        name_item = self.table.item(item.row(), 0)
        directory = name_item.data(Qt.UserRole) if name_item else None
        if directory:
            self.openRequested.emit(Path(str(directory)))

    def _move_rows(self, source_idxs, drop_y: float) -> None:
        """Reordonne : place les lignes glissees a la position de la chute.

        `source_idxs` = indices dans `self.rows` (transportes par le MIME),
        `drop_y` = ordonnee de la chute dans le viewport du tableau. Le
        calcul se fait sur la SEQUENCE VISUELLE (indices reels memorises
        dans les cellules) : on retire les lignes glissees de l'ordre
        affiche, on les re-insere au point de chute, et cet ordre devient
        le nouvel ordre de `self.rows`. Un reordonnancement manuel annule le
        tri actif (incompatible avec lui) : l'ordre recalcule est alors
        exactement l'ordre affiche.
        """
        if not self.rows:
            return
        src = [i for i in sorted({int(i) for i in source_idxs})
               if 0 <= i < len(self.rows)]
        if not src:
            return
        src_set = set(src)
        model = self.table.model()
        # sequence visuelle des indices reels (differe de la liste si trie)
        order: list[int] = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            stored = item.data(Qt.UserRole + 1) if item is not None else None
            if stored is not None:
                order.append(int(stored))
        remaining = [i for i in order if i not in src_set]
        moved = [i for i in order if i in src_set]
        # position de la chute dans la nouvelle sequence
        position = len(remaining)          # defaut : en dessous de tout
        target_visual = self.table.rowAt(int(drop_y))
        if 0 <= target_visual < len(order):
            target_real = order[target_visual]
            # emplace de la cible dans remaining (meme si elle est glissee :
            # c'est la case qu'elle quitte qui compte)
            pos = sum(
                1 for i in order[:target_visual] if i not in src_set
            )
            rect = self.table.visualRect(model.index(target_visual, 0))
            if drop_y > rect.top() + rect.height() / 2.0:
                pos += 1                   # moitie basse -> apres la cible
            position = max(0, min(pos, len(remaining)))
        elif self.table.rowCount() and drop_y < (
                self.table.visualRect(model.index(0, 0)).top()):
            position = 0                   # au-dessus de la 1re ligne
        new_order = remaining[:position] + moved + remaining[position:]
        new_rows = [self.rows[i] for i in new_order]
        # l'ordre manuel est incompatible avec un tri actif : on l'efface,
        # show_rows remet ensuite le tableau en ordre de liste.
        self.table.setSortingEnabled(False)
        self.table.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        self.show_rows(new_rows)
        self.rowsChanged.emit()

    def selected_names(self) -> list[str]:
        """Noms des lignes selectionnees (colonne « Molécule » = colonne 0)."""
        names: list[str] = []
        selection = self.table.selectionModel()
        if selection is None:
            return names
        for index in selection.selectedRows():
            item = self.table.item(index.row(), 0)
            if item is not None:
                names.append(item.text())
        return names


class MoleculeInputDialog(QDialog):
    """Saisie d'un nom de molecule, avec autocompletion locale."""

    def __init__(self, parent, suggestions, title="Ajouter une molécule",
                 label="Nom de la molécule (français ou anglais) :"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(label, self))
        self.edit = QLineEdit(self)
        known = sorted({str(s).strip() for s in (suggestions or [])
                        if str(s).strip()}, key=str.casefold)
        completion = QCompleter(known, self)
        completion.setCaseSensitivity(Qt.CaseInsensitive)
        completion.setFilterMode(Qt.MatchContains)
        self.edit.setCompleter(completion)
        layout.addWidget(self.edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.button(QDialogButtonBox.Ok).setText("Ajouter")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.edit.returnPressed.connect(self.accept)
        layout.addWidget(buttons)

    def molecule(self) -> str:
        return self.edit.text().strip()

    @staticmethod
    def ask(parent, suggestions) -> str:
        """Retourne le nom saisi, ou "" si l'utilisateur annule."""
        dialog = MoleculeInputDialog(parent, suggestions)
        if dialog.exec() == QDialog.Accepted:
            return dialog.molecule()
        return ""


# --- page interactions : croisement de plusieurs molecules -------------------
class MoleculeList(QListWidget):
    """Liste des molecules etudiees ; accepte le glisser de l'explorateur.

    Seul le type MIME interne (PATH_MIME) est accepte : un depot venu
    d'ailleurs (gestionnaire de fichiers, autre application) est ignore.
    """

    pathDropped = Signal(object)
    removeRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.setToolTip(
            "Glissez une molécule depuis l'explorateur pour l'ajouter à "
            "l'étude d'interactions. La touche Suppr retire la sélection."
        )

    def keyPressEvent(self, event) -> None:  # noqa: N802 - API Qt
        """Suppr retire de la liste les molécules sélectionnées."""
        if event.key() == Qt.Key_Delete and not event.modifiers():
            if self.selectedItems():
                self.removeRequested.emit()
                return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - API Qt
        if event.mimeData().hasFormat(PATH_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - API Qt
        if event.mimeData().hasFormat(PATH_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802 - API Qt
        mime = event.mimeData()
        if not mime.hasFormat(PATH_MIME):
            event.ignore()
            return
        try:
            path = bytes(mime.data(PATH_MIME)).decode("utf-8").strip()
        except UnicodeDecodeError:
            event.ignore()
            return
        if not path:
            event.ignore()
            return
        self.pathDropped.emit(Path(path))
        event.acceptProposedAction()


class RankItem(QTableWidgetItem):
    """Cellule de niveau : trie par gravite (et non alphabetiquement)."""

    def __init__(self, text: str, rank: int):
        super().__init__(text)
        self._rank = int(rank)

    def __lt__(self, other) -> bool:  # noqa: D105 - comparaison Qt
        if isinstance(other, RankItem):
            return self._rank < other._rank
        return super().__lt__(other)


class InteractionsPane(QWidget):
    """Croisement des interactions entre plusieurs molecules (page dediee).

    A gauche la liste des molecules etudiees (ajout manuel ou glisser depuis
    l'explorateur) ; a droite une ligne par paire, avec niveau, sources et
    detail. Aucun acces reseau ici : la fenetre fournit les donnees (base
    DDInter locale, resume.json, Wikidata) et met a jour le tableau.
    """

    LEVEL_COLORS = {
        "Contre-indiqué": "#f0b4b4",
        "Majeur": "#f7d2d2",
        "Modéré": "#fbe6c4",
        "Mineur": "#fdf5cf",
        "Mentionné": "#dbe9fb",
        "Aucune": "#f2f2f2",
    }
    COLUMNS = ("Molécule A", "Molécule B", "Niveau", "Mécanisme", "Source",
               "Détail")

    addRequested = Signal()
    removeRequested = Signal()
    clearRequested = Signal()
    refreshRequested = Signal()
    downloadBaseRequested = Signal()
    exportRequested = Signal()
    saveRequested = Signal(object)
    loadRequested = Signal(object)
    useCompareRequested = Signal()
    pathDropped = Signal(object)
    wikidataToggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.rows: list[dict] = []          # toutes les paires calculees
        self._visible: list[dict] = []      # celles affichees apres filtre

        bar = QToolBar(self)
        bar.setIconSize(QSize(16, 16))
        bar.addAction("Ajouter une molécule…").triggered.connect(
            self.addRequested
        )
        bar.addAction("Retirer la sélection").triggered.connect(
            self.removeRequested
        )
        bar.addAction("Vider").triggered.connect(self.clearRequested)
        bar.addSeparator()
        save = QAction("Enregistrer…", self)
        save.setToolTip(
            "Enregistre les deux tableaux (comparaison et interactions) dans "
            "un fichier JSON."
        )
        save.triggered.connect(lambda: self.saveRequested.emit("both"))
        bar.addAction(save)
        save_i = QAction("Enregistrer ce tableau…", self)
        save_i.setToolTip("Enregistre uniquement ce tableau (interactions).")
        save_i.triggered.connect(lambda: self.saveRequested.emit("interactions"))
        bar.addAction(save_i)
        load = QAction("Charger…", self)
        load.setToolTip(
            "Recharge les deux tableaux depuis un fichier JSON enregistré."
        )
        load.triggered.connect(lambda: self.loadRequested.emit("both"))
        bar.addAction(load)
        load_i = QAction("Charger dans ce tableau…", self)
        load_i.setToolTip(
            "Recharge uniquement le tableau d'interactions depuis un fichier JSON."
        )
        load_i.triggered.connect(lambda: self.loadRequested.emit("interactions"))
        bar.addAction(load_i)
        bar.addSeparator()
        compare_btn = QAction("Reprendre la comparaison", self)
        compare_btn.setToolTip(
            "Recopie dans cette page les molécules du tableau de comparaison."
        )
        compare_btn.triggered.connect(self.useCompareRequested)
        bar.addAction(compare_btn)
        refresh = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload),
            "Actualiser", self,
        )
        refresh.setToolTip(
            "Relit les interactions PsychonautWiki locales et la base DDInter."
        )
        refresh.triggered.connect(self.refreshRequested)
        bar.addAction(refresh)
        bar.addSeparator()
        base = QAction("Télécharger la base DDInter", self)
        base.setToolTip(
            "Récupère les 14 fichiers d'interactions DDInter (~30 Mo) dans "
            "le cache local. À faire une seule fois."
        )
        base.triggered.connect(self.downloadBaseRequested)
        bar.addAction(base)
        self.wikidata_action = QAction("Wikidata", self, checkable=True)
        self.wikidata_action.setToolTip(
            "Ajoute les interactions documentées dans Wikidata (propriété "
            "P769, licence CC0). Requête réseau mise en cache 30 jours."
        )
        saved = str(self.settings.value("interactions_wikidata", "true")).lower()
        self.wikidata_action.setChecked(saved in ("true", "1"))
        self.wikidata_action.toggled.connect(self.wikidataToggled)
        bar.addAction(self.wikidata_action)
        export = QAction("Exporter CSV…", self)
        export.setToolTip(
            "Écrit les paires affichées dans un fichier CSV (séparateur « ; »)."
        )
        export.triggered.connect(self.exportRequested)
        bar.addAction(export)

        self.base_label = QLabel(self)
        self.base_label.setStyleSheet("color: #666; padding: 0 8px;")
        bar.addSeparator()
        bar.addWidget(self.base_label)

        self.count_label = QLabel(self)
        self.count_label.setMinimumWidth(170)
        self.count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bar.addWidget(self.count_label)

        # volet gauche : molecules etudiees
        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.addWidget(QLabel("Molécules étudiées", left))
        self.list = MoleculeList(left)
        self.list.pathDropped.connect(self.pathDropped)
        # Suppr dans la liste = « Retirer la sélection » de la barre d'outils
        self.list.removeRequested.connect(self.removeRequested)
        left_layout.addWidget(self.list, 1)
        hint = QLabel(
            "Glissez une molécule depuis l'explorateur (ou « Ajouter une "
            "molécule… »), puis laissez la base se remplir automatiquement.",
            left,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #777;")
        left_layout.addWidget(hint)

        # volet droit : paires d'interactions
        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 4, 4, 4)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("Niveau :", right))
        self.level_filter = QComboBox(right)
        for label, value in (
            ("Tous les niveaux", "all"),
            ("Majeur et plus", "major"),
            ("Modéré et plus", "moderate"),
            ("Mineur et plus", "minor"),
            ("Aucune interaction", "none"),
        ):
            self.level_filter.addItem(label, value)
        self.level_filter.currentIndexChanged.connect(
            lambda _index: self._apply_filter()
        )
        filters.addWidget(self.level_filter)
        self.hide_none = QCheckBox("Masquer les paires sans interaction", right)
        self.hide_none.toggled.connect(lambda _checked: self._apply_filter())
        filters.addWidget(self.hide_none)
        filters.addStretch(1)
        right_layout.addLayout(filters)

        self.table = QTableWidget(right)
        self.table.setColumnCount(len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSortIndicator(2, Qt.DescendingOrder)
        self.table.horizontalHeader().setStretchLastSection(True)
        mechanism_header = self.table.horizontalHeaderItem(3)
        if mechanism_header is not None:
            mechanism_header.setToolTip(
                "Mécanisme probable, déduit des classes pharmacologiques "
                "(synergie, potentialisation, addition, antagonisme, "
                "métabolisme, toxicité). Classification indicative, ni avis "
                "médical ni conduite à tenir. Personnalisable via "
                "mechanisms.json dans le dossier de cache."
            )
        right_layout.addWidget(self.table, 1)

        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #555; padding: 2px 6px;")
        right_layout.addWidget(self.status)

        self.warning = QLabel(
            "Rapprochement documentaire de trois sources (PsychonautWiki, "
            "DDInter CC BY-NC-SA 4.0, Wikidata CC0) ; la colonne "
            "« Mécanisme » est une classification indicative déduite des "
            "classes pharmacologiques (survol pour le détail) : ni avis "
            "médical, ni conduite à tenir. Vérifiez toujours auprès d'une "
            "source pharmaceutique de référence.",
            self,
        )
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #8a5a00; padding: 2px 6px;")
        right_layout.addWidget(self.warning)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([300, 900])
        self.splitter = splitter

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(bar)
        layout.addWidget(splitter, 1)

        self._apply_filter()

    # -- liste des molecules ----------------------------------------------
    def names(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def set_names(self, names) -> None:
        """Remplace la liste (doublons et vides ecartes, ordre conserve)."""
        seen: set[str] = set()
        clean: list[str] = []
        for name in names or []:
            text = str(name).strip()
            key = rm.fold_accents(text).casefold()
            if text and key not in seen:
                seen.add(key)
                clean.append(text)
        self.list.clear()
        for name in clean:
            self.list.addItem(QListWidgetItem(name))

    def add_names(self, names) -> None:
        existing = {rm.fold_accents(n).casefold() for n in self.names()}
        for name in names or []:
            text = str(name).strip()
            key = rm.fold_accents(text).casefold()
            if text and key not in existing:
                existing.add(key)
                self.list.addItem(QListWidgetItem(text))

    def selected_names(self) -> list[str]:
        return [item.text() for item in self.list.selectedItems()]

    def wikidata_enabled(self) -> bool:
        return bool(self.wikidata_action.isChecked())

    def visible_rows(self) -> list[dict]:
        return list(self._visible)

    # -- tableau ----------------------------------------------------------
    def show_rows(self, rows) -> None:
        self.rows = [dict(row) for row in (rows or [])]
        self._apply_filter()

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def _apply_filter(self) -> None:
        mode = str(self.level_filter.currentData() or "all")
        minimum = {"major": 3, "moderate": 2, "minor": 1}.get(mode)
        rows = list(self.rows)
        if mode == "none":
            rows = [r for r in rows if int(r.get("rank", 0)) == 0]
        elif minimum is not None:
            rows = [r for r in rows if int(r.get("rank", 0)) >= minimum]
        if self.hide_none.isChecked():
            rows = [r for r in rows if int(r.get("rank", 0)) > 0]
        self._visible = rows
        self._fill(rows)

    def _fill(self, rows) -> None:
        header = self.table.horizontalHeader()
        section = header.sortIndicatorSection()
        order = header.sortIndicatorOrder()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            label = str(row.get("level_label", "Aucune"))
            mechanism = str(row.get("mechanism", ""))
            mechanism_note = str(row.get("mechanism_note", ""))
            values = (
                str(row.get("a", "")),
                str(row.get("b", "")),
                label,
                mechanism,
                str(row.get("sources", "")),
                str(row.get("detail", "")),
            )
            for c, text in enumerate(values):
                item = (RankItem(text, int(row.get("rank", 0)))
                        if c == 2 else QTableWidgetItem(text))
                if c == 3 and mechanism_note:
                    item.setToolTip(f"{mechanism} — {mechanism_note}")
                else:
                    item.setToolTip(text)
                self.table.setItem(r, c, item)
            color = self.LEVEL_COLORS.get(label)
            if color:
                for c in (0, 1, 2):
                    cell = self.table.item(r, c)
                    if cell is not None:
                        cell.setBackground(QBrush(QColor(color)))
        self.table.setSortingEnabled(True)
        if section >= 0:
            self.table.sortItems(section, order)
        for c, width in enumerate((160, 160, 110, 150, 170, 260)):
            self.table.setColumnWidth(c, width)
        self._update_count()

    def _update_count(self) -> None:
        total = len(self.rows)
        shown = len(self._visible)
        flagged = sum(1 for row in self._visible if row.get("known"))
        if not total:
            self.count_label.setText("Aucune paire")
            self.count_label.setToolTip(
                "Ajoutez au moins deux molécules pour croiser leurs "
                "interactions."
            )
            return
        self.count_label.setText(f"{shown}/{total} paire(s), {flagged} signalée(s)")
        self.count_label.setToolTip(
            "Paires affichées / paires calculées, et nombre de paires pour "
            "lesquelles au moins une source signale une interaction."
        )


# --- fenetre principale -------------------------------------------------------
class MainWindow(QMainWindow):
    """Les trois volets : explorateur, lecteur PDF, resume."""

    def __init__(self, root: Path | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(APP_NAME)
        self.resize(1500, 940)

        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(4)
        self._task: Task | None = None
        self._workers: list[Worker] = []
        self.settings = QSettings(ORG_NAME, APP_NAME)

        self.workspace = WorkspacePane(Path.cwd())
        self.tabs = QTabWidget(self)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.summary = SummaryPane(self)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self.workspace)
        splitter.addWidget(self.tabs)
        splitter.addWidget(self.summary)
        splitter.setStretchFactor(0, 1)  # explorateur un peu plus large
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([340, 740, 420])
        self.splitter = splitter

        # page « Comparaison » : le meme explorateur (deplace d'une page a
        # l'autre) + un tableau comparatif qui remplace lecteur et resume.
        self._compare_queue: list[dict] = []
        self._compare_items_cache: list[dict] | None = None
        self._research_sizes: list[int] = []
        self.compare = ComparePane(self)
        self.compare.addRequested.connect(self._compare_add_dialog)
        self.compare.removeRequested.connect(self._compare_remove_selected)
        self.compare.clearRequested.connect(self._compare_clear)
        self.compare.downloadMissingRequested.connect(
            self._compare_download_missing
        )
        self.compare.refreshRequested.connect(self._compare_reload)
        self.compare.saveRequested.connect(self.save_tables)
        self.compare.loadRequested.connect(self.load_tables)
        self.compare.openRequested.connect(self._compare_open)
        # glisser-deposer depuis l'explorateur + reordonnancement manuel
        self.compare.pathDropped.connect(self._compare_add_from_tree)
        self.compare.rowsChanged.connect(self._compare_persist)
        self.compare_splitter = QSplitter(Qt.Horizontal, self)
        self.compare_splitter.addWidget(self.compare)
        self.compare_splitter.setStretchFactor(0, 1)
        self.compare_splitter.setStretchFactor(1, 3)
        self.compare_splitter.setSizes([340, 960])

        self.pages = QTabWidget(self)
        self.pages.setDocumentMode(True)
        self.pages.addTab(self.splitter, "Recherche")
        self.pages.addTab(self.compare_splitter, "Comparaison")

        # page « Interactions » : croisement de plusieurs molecules (trois
        # sources : resume.json locaux, base DDInter, Wikidata).
        self._interactions_cache: list[str] | None = None
        self._ddinter_index_cache: dict | None = None
        self._interactions_last_report = ""
        self._resume_auto_attempted: set[str] = set()
        self._wd_pairs: dict = {}
        self._wd_pending = False
        self._wd_names: tuple[str, ...] = ()
        self._wd_done_names: tuple[str, ...] = ()
        self._wd_generation = 0
        self.interactions = InteractionsPane(self)
        self.interactions.addRequested.connect(self._interactions_add_dialog)
        self.interactions.removeRequested.connect(
            self._interactions_remove_selected
        )
        self.interactions.clearRequested.connect(self._interactions_clear)
        self.interactions.refreshRequested.connect(self._interactions_refresh)
        self.interactions.downloadBaseRequested.connect(
            self._interactions_download_base
        )
        self.interactions.exportRequested.connect(self._interactions_export)
        self.interactions.saveRequested.connect(self.save_tables)
        self.interactions.loadRequested.connect(self.load_tables)
        self.interactions.useCompareRequested.connect(
            self._interactions_use_compare
        )
        self.interactions.pathDropped.connect(self._interactions_add_from_tree)
        self.interactions.wikidataToggled.connect(
            self._interactions_wikidata_toggled
        )
        self.interactions_splitter = QSplitter(Qt.Horizontal, self)
        self.interactions_splitter.addWidget(self.interactions)
        self.interactions_splitter.setStretchFactor(0, 1)
        self.interactions_splitter.setStretchFactor(1, 3)
        self.interactions_splitter.setSizes([340, 940])
        self.pages.addTab(self.interactions_splitter, "Interactions")

        # non fermables : trois onglets permanents, comme des modes de la fenetre
        self.pages.currentChanged.connect(self._page_changed)
        self.setCentralWidget(self.pages)

        self.progress = QProgressBar(self)
        self.progress.setMaximumWidth(220)
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)

        self._build_menus()
        self._connect_workspace()

        self.welcome: WelcomePane | None = None
        self.root: Path | None = None
        if root is not None:
            self.set_workspace(root)
        else:
            self._show_welcome()

    # -- menus -----------------------------------------------------------
    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&Fichier")
        open_action = QAction("Ouvrir un répertoire de travail…", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self.choose_workspace)
        file_menu.addAction(open_action)
        file_menu.addSeparator()
        save_tables = QAction("Enregistrer les tableaux…", self)
        save_tables.setShortcut("Ctrl+Shift+S")
        save_tables.setToolTip(
            "Enregistre les tableaux de comparaison et d'interactions dans un "
            "fichier JSON."
        )
        save_tables.triggered.connect(self.save_tables)
        file_menu.addAction(save_tables)
        load_tables = QAction("Charger les tableaux…", self)
        load_tables.setShortcut("Ctrl+Shift+O")
        load_tables.setToolTip(
            "Recharge les tableaux de comparaison et d'interactions depuis un "
            "fichier JSON enregistré."
        )
        load_tables.triggered.connect(self.load_tables)
        file_menu.addAction(load_tables)
        file_menu.addSeparator()
        close_tab = QAction("Fermer l'onglet", self)
        close_tab.setShortcut(QKeySequence.Close)
        close_tab.triggered.connect(
            lambda: self._close_tab(self.tabs.currentIndex())
        )
        file_menu.addAction(close_tab)
        quit_action = QAction("Quitter", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        search_menu = self.menuBar().addMenu("&Recherche")
        find_action = QAction("Rechercher dans le document", self)
        find_action.setShortcut(QKeySequence.Find)
        find_action.triggered.connect(self._focus_search)
        search_menu.addAction(find_action)
        pubmed_action = QAction("Rechercher un article ou un livre…", self)
        pubmed_action.setShortcut("Ctrl+L")
        pubmed_action.triggered.connect(
            lambda: self.search_literature(self.workspace.current_dir())
        )
        search_menu.addAction(pubmed_action)
        mol_action = QAction("Ajouter une molécule…", self)
        mol_action.setShortcut("Ctrl+M")
        mol_action.triggered.connect(
            lambda: self.add_molecule(self.workspace.current_dir())
        )
        search_menu.addAction(mol_action)

        view_menu = self.menuBar().addMenu("&Affichage")
        self.toggle_left = QAction("Explorateur", self, checkable=True, checked=True)
        self.toggle_left.toggled.connect(
            lambda shown: self.workspace.setVisible(shown)
        )
        view_menu.addAction(self.toggle_left)
        self.toggle_right = QAction("Résumé", self, checkable=True, checked=True)
        self.toggle_right.toggled.connect(lambda shown: self.summary.setVisible(shown))
        view_menu.addAction(self.toggle_right)
        view_menu.addSeparator()
        page_recherche = QAction("Page Recherche", self)
        page_recherche.setShortcut("Ctrl+1")
        page_recherche.triggered.connect(lambda: self.show_page(0))
        view_menu.addAction(page_recherche)
        page_compare = QAction("Page Comparaison", self)
        page_compare.setShortcut("Ctrl+2")
        page_compare.triggered.connect(lambda: self.show_page(1))
        view_menu.addAction(page_compare)
        page_inter = QAction("Page Interactions", self)
        page_inter.setShortcut("Ctrl+3")
        page_inter.triggered.connect(lambda: self.show_page(2))
        view_menu.addAction(page_inter)

        help_menu = self.menuBar().addMenu("&Aide")
        about = QAction("À propos", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _about(self) -> None:
        QMessageBox.information(self, f"À propos de {APP_NAME}", DISCLAIMER)

    def _connect_workspace(self) -> None:
        ws = self.workspace
        ws.documentActivated.connect(self.open_document)
        ws.pathActivated.connect(self._open_externally)
        # survol/selection dans l'explorateur : le resume se met a jour avant
        # meme d'ouvrir le fichier.
        ws.pathSelected.connect(self._preview_summary)
        ws.requestNewSubdir.connect(self.new_subdirectory)
        ws.requestWikipediaPdf.connect(self.add_wikipedia_pdf)
        ws.requestPsychonautWikiPdf.connect(self.add_psychonautwiki_pdf)
        ws.requestExistingPdf.connect(self.add_existing_pdf)
        ws.requestLiterature.connect(self.search_literature)
        ws.requestMolecule.connect(self.add_molecule)
        ws.requestAssociate.connect(self.associate_molecule)
        ws.requestRefreshResume.connect(self.refresh_resume)
        ws.requestDelete.connect(self._delete)
        ws.requestOpenInFileManager.connect(self._open_externally)
        ws.requestCompare.connect(self._compare_add_from_tree)
        ws.requestInteractions.connect(self._interactions_add_from_tree)
        # le bouton « Régénérer le résumé » du volet droit relance le parser
        # sur le dossier de la molécule affichée (même voie que le menu
        # contextuel de l'explorateur).
        self.summary.refreshRequested.connect(self.refresh_resume)

    # -- repertoire de travail -------------------------------------------
    def choose_workspace(self) -> None:
        start = str(self.root) if self.root else str(Path.home())
        directory = QFileDialog.getExistingDirectory(
            self, "Choisir le répertoire de travail", start
        )
        if directory:
            self.set_workspace(Path(directory))

    def set_workspace(self, root: Path) -> None:
        root = Path(root).expanduser()
        if not root.is_dir():
            QMessageBox.warning(
                self, "Répertoire introuvable", f"Impossible d'ouvrir : {root}"
            )
            self._show_welcome()
            return
        self.root = root
        self.setWindowTitle(f"{APP_NAME} — {root}")
        self.workspace.set_root(root)
        self.settings.setValue("workspace", str(root))
        self._remove_welcome()
        self.summary.clear()
        self._compare_reload()
        self.statusBar().showMessage(f"Répertoire de travail : {root}", 5000)

    def _show_welcome(self) -> None:
        if self.welcome is None:
            self.welcome = WelcomePane(self)
            self.welcome.chooseRequested.connect(self.choose_workspace)
        index = self.tabs.addTab(self.welcome, "Accueil")
        self.tabs.tabBar().setTabButton(
            index, QTabBar.ButtonPosition.RightSide, None
        )

    def _remove_welcome(self) -> None:
        if self.welcome is None:
            return
        index = self.tabs.indexOf(self.welcome)
        if index >= 0:
            self.tabs.removeTab(index)
        self.welcome.deleteLater()
        self.welcome = None

    # -- taches de fond --------------------------------------------------
    def _start_task(self, task: Task) -> None:
        if self._task is not None:
            QMessageBox.information(
                self, "Patientez", "Une opération est déjà en cours."
            )
            return
        self._task = task
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self.statusBar().showMessage(task.label)

        worker = Worker(
            lambda progress, fraction: task.build(task.choices, progress, fraction)
        )
        self._workers.append(worker)
        worker.signals.message.connect(self.statusBar().showMessage)
        worker.signals.fraction.connect(self._on_fraction)
        worker.signals.ambiguous.connect(lambda exc: self._on_ambiguous(task, exc))
        worker.signals.failed.connect(lambda msg: self._on_failed(task, msg))
        worker.signals.finished.connect(lambda result: self._on_finished(task, result))
        self.pool.start(worker)

    def _end_task(self) -> None:
        self._task = None
        self.progress.setVisible(False)
        self.progress.setRange(0, 100)

    @Slot(float)
    def _on_fraction(self, value: float) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(int(max(0.0, min(value, 1.0)) * 100))

    def _on_ambiguous(self, task: Task, exc: rm.AmbiguousResults) -> None:
        self._end_task()
        choice = ChooseTitleDialog.ask(self, exc)
        if not choice:
            self.statusBar().showMessage("Opération annulée.", 6000)
            self._compare_pump()
            return
        task.choices[exc.source] = choice
        self._start_task(task)

    def _on_failed(self, task: Task, message: str) -> None:
        self._end_task()
        if getattr(task, "quiet_fail", False):
            # telechargement de la file de comparaison : pas de fenetre par
            # echec, l'etat de la ligne et la barre d'etat suffisent.
            self.statusBar().showMessage(f"Échec : {message}", 8000)
        else:
            self.statusBar().showMessage("Échec de l'opération.", 6000)
            QMessageBox.warning(self, "Opération impossible", message)
        if task.on_fail is not None:
            task.on_fail(message)
        self._compare_pump()

    def _on_finished(self, task: Task, result) -> None:
        self._end_task()
        self.workspace.reload()
        self.statusBar().showMessage("Terminé.", 4000)
        if task.on_done is not None:
            try:
                task.on_done(result)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Erreur d'affichage", str(exc))
        self._compare_pump()

    # -- actions de l'explorateur ----------------------------------------
    def _require_workspace(self) -> bool:
        """Verifie qu'un repertoire de travail est ouvert avant d'agir."""
        if self.root is None:
            QMessageBox.information(
                self,
                "Aucun répertoire de travail",
                "Choisissez d'abord un répertoire de travail "
                "(menu Fichier → Ouvrir un répertoire de travail…).",
            )
            return False
        return True

    def new_subdirectory(self, directory: Path) -> None:
        if not self._require_workspace():
            return
        name, ok = QInputDialog.getText(
            self, "Nouveau sous-dossier", "Nom du dossier :"
        )
        if not ok or not name.strip():
            return
        try:
            created = sb.create_subdirectory(self.root, directory, name.strip())
        except (sb.WorkspaceError, sb.AcquireError) as exc:
            QMessageBox.warning(self, "Création impossible", str(exc))
            return
        self.workspace.reload()
        self.statusBar().showMessage(f"Dossier créé : {created.name}", 5000)

    def add_wikipedia_pdf(self, directory: Path) -> None:
        if not self._require_workspace():
            return
        query, ok = QInputDialog.getText(
            self,
            "PDF d'une page Wikipédia",
            "Titre (ou termes de recherche) de la page Wikipédia anglaise :",
        )
        if not ok or not query.strip():
            return
        query = query.strip()
        target_dir = Path(directory)

        def build(choices, progress, fraction):
            title = sb.resolve_wikipedia_title(query, choices.get("wp", ""))
            progress(f"Téléchargement du PDF Wikipédia « {title} »…")
            return sb.download_wikipedia_pdf(title, target_dir, fraction=fraction)

        def done(path):
            self.open_document(Path(path))

        self._start_task(Task(f"PDF Wikipédia : {query}", build, done))

    def add_psychonautwiki_pdf(self, directory: Path) -> None:
        """Page PsychonautWiki -> PDF, dans le dossier demande."""
        if not self._require_workspace():
            return
        query, ok = QInputDialog.getText(
            self,
            "PDF d'une page PsychonautWiki",
            "Nom de la page PsychonautWiki (ex. : LSD, MDMA, Ketamine…) :",
        )
        if not ok or not query.strip():
            return
        query = query.strip()
        target_dir = Path(directory)

        def build(choices, progress, fraction):
            title = sb.resolve_psychonautwiki_title(
                query, choices.get("pw", ""), progress
            )
            html = sb.psychonautwiki_html(title, progress)
            progress(f"Mise en page du PDF « {title} » …")
            printable = sb.html_for_print(
                html, title=title, source_label="PsychonautWiki",
                source_url=sb.psychonautwiki_page_url(title),
                fetch_images=True, progress=progress,
            )
            dest = sb.unique_path(target_dir / sb.psychonautwiki_pdf_name(title))
            progress(f"Écriture du PDF PsychonautWiki « {title} »…")
            return render_html_pdf(printable, dest, title=title)

        def done(path):
            self.open_document(Path(path))

        self._start_task(Task(f"PDF PsychonautWiki : {query}", build, done))

    def add_existing_pdf(self, directory: Path) -> None:
        if not self._require_workspace():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Ajouter un document existant", str(Path.home()),
            "Documents (PDF, texte, images, HTML, EPUB) "
            "(*.pdf *.txt *.md *.csv *.json *.bib *.png *.jpg *.jpeg *.gif "
            "*.html *.htm *.epub);;Tous les fichiers (*)",
        )
        if not path:
            return
        try:
            target = sb.import_document(self.root, directory, Path(path))
        except (sb.WorkspaceError, sb.AcquireError) as exc:
            QMessageBox.warning(self, "Import impossible", str(exc))
            return
        self.workspace.reload()
        self.open_document(target)

    def search_literature(self, directory: Path) -> None:
        if not self._require_workspace():
            return
        dialog = BookSearchDialog(self, directory, root=self.root)
        dialog.exec()
        if dialog.downloaded is not None:
            self.workspace.reload()
            self.open_document(dialog.downloaded)

    def add_molecule(self, directory: Path) -> None:
        if not self._require_workspace():
            return
        molecule, ok = QInputDialog.getText(
            self,
            "Ajouter une molécule",
            "Nom de la molécule (français ou anglais) :",
        )
        if not ok or not molecule.strip():
            return
        molecule = molecule.strip()
        target_dir = Path(directory)

        def done(result):
            if result.get("pdf"):
                self.open_document(Path(result["pdf"]))
            elif result.get("pw_pdf"):
                self.open_document(Path(result["pw_pdf"]))
            else:
                self.summary.show_molecule(result["dir"], molecule)
                QMessageBox.information(
                    self,
                    "Molécule ajoutée",
                    f"Dossier « {result['dir'].name} » créé avec son résumé, "
                    "mais le PDF Wikipédia n'a pas pu être téléchargé.\n"
                    "Vous pouvez l'ajouter plus tard avec « Ajouter le PDF "
                    "d'une page Wikipédia… ».",
                )

        self._start_task(Task(
            f"Ajout de la molécule {molecule}",
            self._molecule_build(target_dir, molecule), done,
        ))

    def _molecule_build(self, target_dir: Path, molecule: str):
        """Fabrique la tache « ajouter une molecule » : dossier + resume +
        PDFs (Wikipedia et rendu PsychonautWiki). Partagee par le bouton
        « + » de l'explorateur et par la file de la page Comparaison."""

        def build(choices, progress, fraction):
            result = sb.add_molecule(
                self.root, target_dir, molecule,
                choices=choices, progress=progress, fraction=fraction,
            )
            # PDF PsychonautWiki : rendu a partir de la page deja recuperee
            # par le parser (pas de requete supplementaire), avec images
            # integrees et feuille de style d'impression.
            pw_html = result.get("pw_html") or ""
            pw_title = result.get("pw_title") or ""
            if pw_html and pw_title:
                progress(f"Mise en page du PDF PsychonautWiki « {pw_title} » …")
                printable = sb.html_for_print(
                    pw_html, title=pw_title, source_label="PsychonautWiki",
                    source_url=sb.psychonautwiki_page_url(pw_title),
                    fetch_images=True, progress=progress,
                )
                dest = sb.unique_path(
                    Path(result["dir"]) / sb.psychonautwiki_pdf_name(pw_title)
                )
                result["pw_pdf"] = render_html_pdf(printable, dest, title=pw_title)
            return result

        return build

    def associate_molecule(self, directory: Path) -> None:
        if not self._require_workspace():
            return
        directory = Path(directory)
        current = sb.read_molecule_meta(directory)
        dialog = ChooseMoleculeDialog(self, current)
        if dialog.exec() != QDialog.Accepted:
            return
        molecule = dialog.molecule()
        if not molecule:
            return
        try:
            sb.write_molecule_meta(directory, molecule)
        except OSError as exc:
            QMessageBox.warning(self, "Écriture impossible", str(exc))
            return
        self.workspace.reload()
        self.statusBar().showMessage(
            f"« {directory.name} » est désormais lié à {molecule}.", 5000
        )
        self._sync_summary_from_current_tab()

    def refresh_resume(self, directory: Path) -> None:
        if not self._require_workspace():
            return
        if not directory:
            return
        directory = Path(directory)
        molecule = sb.read_molecule_meta(directory) or directory.name

        def build(choices, progress, fraction):
            return sb.refresh_resume(directory, molecule, choices, progress)

        def done(_report):
            current = self.summary.molecule_dir
            # ne pas ecraser le volet droit si l'utilisateur regarde
            # entre-temps une autre molecule. Chemins resoles : un dossier
            # atteint par un lien symbolique reste le meme dossier.
            try:
                same = (current is not None
                        and Path(current).resolve() == directory.resolve())
            except OSError:
                same = current is not None and Path(current) == directory
            if same:
                self.summary.show_molecule(directory, molecule)
            self.statusBar().clearMessage()

        self._start_task(Task(f"Résumé de {molecule}", build, done))

    def _delete(self, path: Path) -> None:
        if not self._require_workspace():
            return
        path = Path(path)
        answer = QMessageBox.question(
            self,
            "Placer dans la corbeille",
            f"Envoyer « {path.name} » à la corbeille ?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            # pas d'API « corbeille » portable : deplacement vers un dossier
            # cache du repertoire de travail, ce qui reste reversible.
            trash = sb.safe_path(self.root, ".corbeille")
            trash.mkdir(exist_ok=True)
            target = sb.unique_path(trash / path.name)
            shutil.move(str(path), str(target))
        except (OSError, sb.WorkspaceError) as exc:
            QMessageBox.warning(self, "Suppression impossible", str(exc))
            return
        self.workspace.reload()
        self.statusBar().showMessage(f"« {path.name} » placé dans .corbeille", 5000)

    def _open_externally(self, path: Path) -> None:
        path = Path(path)
        if self.root is not None and path.is_file():
            # ouvrir un fichier lie a une molecule met aussi a jour le resume,
            # meme si l'application par defaut prend le relais pour l'affichage
            self._sync_summary(path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # -- onglets documents ---------------------------------------------------
    def open_document(self, path: Path) -> None:
        """Ouvre un document dans l'onglet central : PDF, texte, HTML, image,
        EPUB. Les formats non pris en charge partent vers l'application du
        systeme (c'est aussi ce qui arrive aux PDF si Qt refuse de les lire)."""
        path = Path(path)
        if not path.is_file():
            QMessageBox.warning(self, "Fichier introuvable", str(path))
            return
        # toute ouverture de document dans l'application ramene a la page
        # Recherche (la page Comparaison n'affiche pas de lecteur).
        if self.pages.currentIndex() != 0:
            self.pages.setCurrentIndex(0)
        kind = sb.document_kind(path)
        if kind == "other":
            self._sync_summary(path)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            return
        self._remove_welcome()
        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if isinstance(widget, DocumentPane) and widget.path == path:
                self.tabs.setCurrentIndex(index)
                # meme onglet deja ouvert : le resume est resynchronise au cas
                # ou le volet droit montrerait encore une autre molecule.
                self._sync_summary(path)
                return
        if kind == "epub":
            self._open_epub(path)
            return
        pane = self._make_pane(path, kind)
        if pane is None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            return
        self._show_pane(pane)

    # nom historique : « ouvrir » reste le meme geste pour tout document
    open_pdf = open_document

    @staticmethod
    def _make_pane(path: Path, kind: str, html: str | None = None):
        if kind == "pdf":
            return PdfPane(path, None)
        if kind == "text":
            return TextPane(path, None)
        if kind == "html":
            return HtmlPane(path, html=html)
        if kind == "image":
            return ImagePane(path, None)
        return None

    def _show_pane(self, pane) -> None:
        index = self.tabs.addTab(pane, pane.path.name)
        self.tabs.setTabToolTip(index, str(pane.path))
        self.tabs.setCurrentIndex(index)
        self._sync_summary(pane.path)

    def _open_epub(self, path: Path) -> None:
        """Convertit l'EPUB en HTML autonome dans un thread de fond."""

        def build(choices, progress, fraction):
            progress(f"Lecture de l'EPUB {path.name}…")
            return sb.epub_to_html(Path(path))

        def done(html):
            self._show_pane(HtmlPane(Path(path), html=html))

        self._start_task(Task(f"Ouverture de {path.name}", build, done))

    def _close_tab(self, index: int) -> None:
        if index < 0:
            return
        widget = self.tabs.widget(index)
        if widget is self.welcome:
            return
        self.tabs.removeTab(index)
        widget.deleteLater()
        if not self._has_document_tabs():
            self.summary.clear()

    def _has_document_tabs(self) -> bool:
        return any(
            isinstance(self.tabs.widget(i), DocumentPane)
            for i in range(self.tabs.count())
        )

    # nom historique conserve pour les scripts de test
    _has_pdf_tabs = _has_document_tabs

    def _tab_changed(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if isinstance(widget, DocumentPane):
            self._sync_summary(widget.path)

    def _sync_summary_from_current_tab(self) -> None:
        widget = self.tabs.currentWidget()
        if isinstance(widget, DocumentPane):
            self._sync_summary(widget.path)

    def _sync_summary(self, path: Path) -> None:
        if self.root is None:
            return
        directory, molecule = sb.locate_molecule(path, self.root)
        if directory is None or not molecule:
            self.summary.show_message(
                "Aucune molécule associée",
                "Ce document n'est pas dans un dossier de molécule. Utilisez "
                "« Associer à une molécule… » (clic droit sur le dossier) ou "
                "créez un dossier de molécule avec le bouton « + ».",
            )
            return
        self.summary.show_molecule(directory, molecule)
        self._maybe_autobuild_resume(directory, molecule)

    def _preview_summary(self, path: Path) -> None:
        """Met a jour le resume pour l'element pointe dans l'explorateur.

        Simple selection (pas d'ouverture) : un dossier de molecule affiche
        son resume, un fichier celui de la molecule qui le contient. Un
        element hors molecule ne touche pas au volet droit, pour qu'un clic
        sur un dossier quelconque ne vide pas le resume affiche.

        Tant qu'un document est ouvert, c'est **sa** molecule qui commande :
        le modele de l'explorateur se remplit de facon asynchrone et peut
        signaler une selection au moment ou un autre document est lu — sans
        cette regle, le resume du document lu etait parfois remplace par
        celui d'une autre molecule.
        """
        if self.root is None:
            return
        if isinstance(self.tabs.currentWidget(), DocumentPane):
            return
        path = Path(path)
        if path.is_dir():
            declared = sb.read_molecule_meta(path)
            if declared or sb.has_resume(path):
                molecule = declared or path.name
                self.summary.show_molecule(path, molecule)
                self._maybe_autobuild_resume(path, molecule)
            return
        directory, molecule = sb.locate_molecule(path, self.root)
        if directory is not None and molecule:
            self.summary.show_molecule(directory, molecule)
            self._maybe_autobuild_resume(directory, molecule)

    def _maybe_autobuild_resume(self, directory: Path, molecule: str) -> None:
        """Genere automatiquement le resume d'une molecule qui n'en a pas.

        Reserve aux dossiers **explicitement associes** a une molécule
        (`.pharma.json`) : sans cette garde, un simple sous-dossier de
        documents serait pris pour une molécule et son nom serait cherché
        en ligne à tort. Une seule tentative par dossier et par session, et
        seulement si aucune autre tache n'occupe le moteur. Hors ligne, le
        bouton *Régénérer le résumé* reste disponible.
        """
        directory = Path(directory)
        if sb.has_resume(directory):
            return
        if not sb.read_molecule_meta(directory):
            # dossier generique (repli du nom) : on ne devine pas une molecule
            return
        key = str(directory.resolve())
        if key in self._resume_auto_attempted or self._task is not None:
            return
        self._resume_auto_attempted.add(key)
        self.refresh_resume(directory)
        if self._task is not None:
            self.summary.show_loading(molecule)
            # message plus explicite que le libelle generique de la tache
            self.statusBar().showMessage(
                f"Récupération automatique du résumé de {molecule}…", 0
            )

    def _focus_search(self) -> None:
        widget = self.tabs.currentWidget()
        if isinstance(widget, DocumentPane):
            widget.focus_search()

    # -- fermeture -------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 - API Qt
        # revenir sur la page Recherche pour que l'explorateur soit de
        # retour dans self.splitter avant la sauvegarde de son etat.
        if self.pages.currentIndex() != 0:
            self.pages.setCurrentIndex(0)
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
        self.settings.setValue("splitter", self.splitter.saveState())
        super().closeEvent(event)

    # -- bascule entre pages ----------------------------------------------
    def show_page(self, index: int) -> None:
        """Bascule entre Recherche (0), Comparaison (1) et Interactions (2)."""
        self.pages.setCurrentIndex(max(0, min(int(index), 2)))

    def _page_changed(self, index: int) -> None:
        """Un seul explorateur : il est deplace dans la page active.

        QSplitter n'a pas de « removeWidget » : le simple fait d'inserer le
        widget dans l'autre splitter le reparente (l'ancien splitter le perd
        de lui-meme).
        """
        if index in (1, 2):
            target = (self.compare_splitter if index == 1
                      else self.interactions_splitter)
            if self.workspace.parent() is not target:
                if self.workspace.parent() is self.splitter:
                    self._research_sizes = self.splitter.sizes()
                target.insertWidget(0, self.workspace)
                target.setSizes([340, 920])
            self.workspace.setVisible(self.toggle_left.isChecked())
            if index == 1:
                self._compare_activate()
            else:
                self._interactions_activate()
        else:
            if self.workspace.parent() is not self.splitter:
                self.splitter.insertWidget(0, self.workspace)
                if len(self._research_sizes) == 3:
                    self.splitter.setSizes(self._research_sizes)
            self.workspace.setVisible(self.toggle_left.isChecked())
            # de retour en Recherche : le volet droit suit l'onglet courant
            self._sync_summary_from_current_tab()

    def _compare_items(self) -> list[dict]:
        """Liste persistee (nom + dossier) du tableau de comparaison."""
        if self._compare_items_cache is None:
            raw = self.settings.value("compare_list", "[]")
            try:
                items = json.loads(str(raw))
            except ValueError:
                items = []
            self._compare_items_cache = [
                {"name": str(i.get("name", "")),
                 "dir": str(i["dir"]) if i.get("dir") else None}
                for i in items
                if isinstance(i, dict) and str(i.get("name", "")).strip()
            ]
        return self._compare_items_cache

    def _compare_persist(self) -> None:
        """Ecrit la liste (noms + dossiers) dans les preferences."""
        items = [{"name": str(row.get("name", "")), "dir": row.get("dir")}
                 for row in self.compare.rows]
        self._compare_items_cache = items
        self.settings.setValue(
            "compare_list", json.dumps(items, ensure_ascii=False)
        )

    def _compare_activate(self) -> None:
        """A chaque passage sur la page : recharge le tableau depuis le
        disque, puis met en file les molecules absentes (telechargement
        automatique a l'ouverture de la page)."""
        if self.root is None:
            self.compare.show_rows([])
            self.statusBar().showMessage(
                "Choisissez d'abord un répertoire de travail "
                "(menu Fichier).", 6000
            )
            return
        self._compare_reload()
        for row in self.compare.rows:
            if row.get("state") == "absente":
                self._compare_enqueue(row)
        self._compare_pump()

    def _compare_reload(self) -> None:
        """Reconstruit le tableau : liste persistee + resume.json du disque."""
        if self.root is None:
            self.compare.show_rows([])
            return
        on_disk = sb.comparison_rows(self.root)
        by_path = {entry["path"]: entry for entry in on_disk}
        by_name = {rm.fold_accents(entry["name"]).casefold(): entry
                   for entry in on_disk}
        rows = []
        for item in self._compare_items():
            name = str(item.get("name", ""))
            found = by_path.get(str(item["dir"])) if item.get("dir") else None
            if found is None:
                found = by_name.get(rm.fold_accents(name).casefold())
            if found is not None:
                row = {"name": found["name"], "dir": found["path"],
                       "state": "disponible", "fields": found["fields"]}
            else:
                directory = sb.find_molecule_dir(self.root, name)
                row = {"name": name,
                       "dir": str(directory) if directory else None,
                       "state": "absente", "fields": {}}
            # conserve un etat transitoire (file / telechargement en cours)
            previous = next(
                (r for r in self.compare.rows
                 if rm.fold_accents(str(r.get("name", ""))).casefold()
                 == rm.fold_accents(name).casefold()), None)
            if previous is not None and previous.get("state") in (
                    "en file", "téléchargement…"):
                row["state"] = previous["state"]
            rows.append(row)
        self.compare.show_rows(rows)
        self._compare_persist()

    def _compare_set_state(self, name: str, state: str) -> None:
        """Change l'etat d'une ligne du tableau (et son affichage)."""
        target = rm.fold_accents(str(name)).casefold()
        rows = []
        for row in self.compare.rows:
            copy = dict(row)
            if rm.fold_accents(str(copy.get("name", ""))).casefold() == target:
                copy["state"] = state
            rows.append(copy)
        self.compare.show_rows(rows)

    def _compare_enqueue(self, row: dict) -> None:
        """Met une molecule absente en file de telechargement."""
        name = str(row.get("name", ""))
        if not name:
            return
        if any(item["name"] == name for item in self._compare_queue):
            return
        self._compare_queue.append({"name": name, "dir": row.get("dir")})
        self._compare_set_state(name, "en file")

    def _compare_pump(self) -> None:
        """Lance la molecule suivante de la file si aucune tache ne tourne."""
        if (self._task is not None or not self._compare_queue
                or self.root is None):
            return
        item = self._compare_queue.pop(0)
        name = str(item["name"])
        directory = Path(str(item["dir"])) if item.get("dir") else None

        if directory is None:
            # dossier absent : creation complete (comme le bouton « + »)
            build = self._molecule_build(self.workspace.current_dir(), name)

            def done(result):
                self._compare_finish(item, Path(result["dir"]))

        elif sb.resume_json_path(directory).is_file():
            # le dossier a ete complete entre temps : rien a telecharger
            self._compare_finish(item, directory)
            return

        else:
            # dossier deja cree sans resume : simple regeneration
            def build(choices, progress, fraction):
                return sb.refresh_resume(directory, name, choices, progress)

            def done(_report):
                self._compare_finish(item, directory)

        def failed(_message):
            self._compare_set_state(name, "échec")
            self._compare_persist()

        task = Task(f"Comparaison : {name}", build, done, failed)
        task.quiet_fail = True  # pas de fenetre par echec en file d'attente
        self._compare_set_state(name, "téléchargement…")
        self._start_task(task)

    def _compare_finish(self, item: dict, directory: Path) -> None:
        """Passe une molecule de la file a l'etat « disponible » puis
        enchaîne sur la molecule suivante."""
        name = str(item["name"])
        entry = next(
            (e for e in sb.comparison_rows(self.root)
             if e["path"] == str(directory)), None)
        if entry is None:
            self._compare_set_state(name, "absente")
        else:
            target = rm.fold_accents(name).casefold()
            rows = []
            for row in self.compare.rows:
                copy = dict(row)
                if (rm.fold_accents(str(copy.get("name", ""))).casefold()
                        == target):
                    copy.update(name=entry["name"], dir=entry["path"],
                                state="disponible", fields=entry["fields"])
                rows.append(copy)
            self.compare.show_rows(rows)
            self.statusBar().showMessage(
                f"« {entry['name']} » disponible dans le tableau.", 5000
            )
        self._compare_persist()
        self._compare_pump()

    def _compare_add(self, name: str, directory: Path | None = None) -> None:
        """Ajoute une ligne au tableau ; telecharge la molecule si elle manque."""
        name = str(name).strip()
        if not name or self.root is None:
            return
        target = rm.fold_accents(name).casefold()
        for row in self.compare.rows:
            if rm.fold_accents(str(row.get("name", ""))).casefold() == target:
                self.statusBar().showMessage(
                    f"« {row.get('name')} » est déjà dans le tableau.", 5000
                )
                return
        directory = Path(directory) if directory else sb.find_molecule_dir(
            self.root, name)
        row: dict = {"name": name, "dir": None, "state": "absente",
                     "fields": {}}
        if directory is not None:
            entry = next(
                (e for e in sb.comparison_rows(self.root)
                 if e["path"] == str(directory)), None)
            if entry is not None:
                row = {"name": entry["name"], "dir": entry["path"],
                       "state": "disponible", "fields": entry["fields"]}
            else:
                row = {"name": name, "dir": str(directory),
                       "state": "absente", "fields": {}}
        self.compare.show_rows(list(self.compare.rows) + [row])
        self._compare_persist()
        if row["state"] == "absente":
            self._compare_enqueue(row)
            self._compare_pump()

    def _compare_add_dialog(self) -> None:
        """Bouton « Ajouter une molécule… » de la page Comparaison."""
        if not self._require_workspace():
            return
        name, ok = QInputDialog.getText(
            self, "Ajouter une molécule",
            "Nom de la molécule (français ou anglais) :",
        )
        if ok and name.strip():
            self._compare_add(name.strip())

    def _compare_add_from_tree(self, path) -> None:
        """Clic droit « Ajouter à la comparaison » : resout la molecule."""
        if not self._require_workspace():
            return
        path = Path(path)
        if path.is_dir() and path != self.root:
            # un dossier : sa propre metadonnee / son resume, sinon son nom
            # (locate_molecule ne descend que depuis un fichier)
            name = sb.read_molecule_meta(path) or path.name
            directory: Path | None = path
        else:
            # un fichier : dossier ancetre porteur d'un resume
            directory, name = sb.locate_molecule(path, self.root)
        if not name:
            QMessageBox.information(
                self, "Pas de molécule",
                "Ce chemin n'est associé à aucune molécule. Associez d'abord "
                "le dossier à une molécule (clic droit), ou ajoutez la "
                "molécule depuis la page Comparaison.",
            )
            return
        self._compare_add(name, directory)
        self.show_page(1)

    def _compare_remove_selected(self) -> None:
        names = {
            rm.fold_accents(n).casefold()
            for n in self.compare.selected_names()
        }
        if not names:
            return
        self._compare_queue = [
            item for item in self._compare_queue
            if rm.fold_accents(str(item["name"])).casefold() not in names
        ]
        rows = [
            row for row in self.compare.rows
            if rm.fold_accents(str(row.get("name", ""))).casefold()
            not in names
        ]
        self.compare.show_rows(rows)
        self._compare_persist()

    def _compare_clear(self) -> None:
        if not self.compare.rows:
            return
        answer = QMessageBox.question(
            self, "Vider le tableau",
            f"Retirer les {len(self.compare.rows)} molécule(s) du tableau ?\n"
            "(Les dossiers du répertoire de travail ne sont pas supprimés.)",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._compare_queue = []
        self.compare.show_rows([])
        self._compare_persist()

    def _compare_download_missing(self) -> None:
        """Bouton « Télécharger les absentes » : (re)met en file les lignes
        absentes ou en echec."""
        if not self._require_workspace():
            return
        pending = [
            row for row in self.compare.rows
            if row.get("state") in ("absente", "échec")
        ]
        for row in pending:
            self._compare_enqueue(row)
        self._compare_pump()
        if pending:
            self.statusBar().showMessage(
                f"{len(pending)} molécule(s) en file de téléchargement.", 5000
            )
        else:
            self.statusBar().showMessage(
                "Toutes les molécules du tableau sont déjà disponibles.",
                5000,
            )

    def _compare_open(self, directory) -> None:
        """Double-clic sur une ligne : ouvre le resume (page Recherche)."""
        resume = sb.resume_html_path(Path(directory))
        if resume.is_file():
            self.open_document(resume)
        else:
            self.statusBar().showMessage(
                "Aucun résumé pour cette molécule : utilisez « Télécharger "
                "les absentes » pour la créer.", 6000,
            )

    # -- page interactions ------------------------------------------------
    def _interactions_names(self) -> list[str]:
        """Liste persistee des molecules etudiees (preferences)."""
        if self._interactions_cache is None:
            raw = self.settings.value("interactions_list", "[]")
            try:
                items = json.loads(str(raw))
            except ValueError:
                items = []
            self._interactions_cache = [str(n) for n in items
                                        if str(n).strip()]
        return self._interactions_cache

    def _interactions_persist(self) -> None:
        names = self.interactions.names()
        self._interactions_cache = list(names)
        self.settings.setValue(
            "interactions_list", json.dumps(names, ensure_ascii=False)
        )

    def _interactions_activate(self) -> None:
        """Passage sur la page : restaure la liste puis recalcule."""
        self.interactions.set_names(self._interactions_names())
        self._interactions_refresh()
        if len(self.interactions.names()) < 2:
            self.statusBar().showMessage(
                "Ajoutez au moins deux molécules pour croiser leurs "
                "interactions.", 6000,
            )

    def _ddinter_index(self) -> dict:
        """Index DDInter de la session (vide si la base est absente)."""
        if self._ddinter_index_cache is None:
            status = sb.ddinter_status()
            self._ddinter_index_cache = (
                sb.ddinter_index() if status["count"] else {}
            )
        return self._ddinter_index_cache

    def _interactions_update_base_label(self) -> None:
        status = sb.ddinter_status()
        if not status["count"]:
            text = "Base DDInter absente"
            tip = ("Cliquez sur « Télécharger la base DDInter » pour récupérer "
                   "les 14 fichiers d'interactions (~30 Mo, licence "
                   "CC BY-NC-SA 4.0). Les interactions PsychonautWiki "
                   "restent disponibles sans elle.")
        elif status["complete"]:
            text = f"Base DDInter : {sb.human_size(status['bytes'])}"
            tip = (f"{status['expected']} fichiers dans {status['dir']}. "
                   "Licence CC BY-NC-SA 4.0 — source : ddinter.scbdd.com")
        else:
            text = (f"Base DDInter : {status['count']}/"
                    f"{status['expected']} fichiers")
            tip = ("Base partiellement téléchargée : relancez "
                   "« Télécharger la base DDInter » pour la compléter.")
        self.interactions.base_label.setText(text)
        self.interactions.base_label.setToolTip(tip)

    def _interaction_classes(self) -> dict:
        """{nom normalisé: [classes]} pour le mécanisme probable (0 réseau)."""
        if self.root is None:
            return {}
        try:
            return sb.workspace_classes(self.root)
        except OSError:
            return {}

    def _interaction_entries(self) -> list[dict]:
        """[{nom, texte interactions PW local}] pour la liste courante."""
        names = self.interactions.names()
        pw = sb.workspace_interactions(self.root) if self.root else {}
        folded = {rm.fold_accents(k).casefold(): v for k, v in pw.items()}
        return [
            {"name": name,
             "text": folded.get(rm.fold_accents(name).casefold(), "")}
            for name in names
        ]

    def _interactions_display(self) -> None:
        """Recalcule et affiche les paires a partir des donnees disponibles."""
        entries = self._interaction_entries()
        if len(entries) < 2:
            self._interactions_last_report = ""
            self.interactions.show_rows([])
            self.interactions.set_status(
                "Ajoutez au moins deux molécules pour croiser leurs "
                "interactions."
            )
            return
        index = self._ddinter_index()
        synonyms = sb.load_synonyms()
        classes = self._interaction_classes()
        mechanisms = sb.load_mechanisms()
        rows = sb.build_interaction_pairs(
            entries, index or None, self._interactions_wd_pairs(), synonyms,
            classes=classes, mechanisms=mechanisms,
        )
        self.interactions.show_rows(rows)
        self._interactions_last_report = self._interactions_report(
            entries, rows, index, synonyms)
        self.interactions.set_status(self._interactions_last_report)

    def _interactions_wd_pairs(self) -> dict:
        return self._wd_pairs if self.interactions.wikidata_enabled() else {}

    def _interactions_report(self, entries, rows, index, synonyms) -> str:
        names = [str(e["name"]) for e in entries]
        flagged = sum(1 for row in rows if row.get("known"))
        parts = [f"{len(rows)} paire(s) pour {len(names)} molécule(s)",
                 f"{flagged} signalée(s)"]
        if index:
            resolved = sb.resolve_ddinter_names(names, index, synonyms)
            missing = [n for n, value in resolved.items() if not value]
            if missing:
                shown = ", ".join(missing[:4])
                if len(missing) > 4:
                    shown += "…"
                parts.append(f"hors couverture DDInter : {shown}")
        else:
            parts.append("base DDInter absente")
        if self._wd_pairs:
            keys = {rm.fold_accents(n).casefold() for n in names}
            wd_count = sum(1 for a, b in self._wd_pairs
                           if a in keys and b in keys)
            if wd_count:
                parts.append(f"Wikidata : {wd_count} paire(s) en cache")
        return " — ".join(parts) + "."

    def _interactions_refresh(self) -> None:
        self._interactions_update_base_label()
        self._interactions_display()
        if self.interactions.wikidata_enabled():
            self._interactions_request_wikidata()

    def _interactions_request_wikidata(self) -> None:
        names = tuple(sorted({n for n in self.interactions.names()},
                             key=str.casefold))
        if len(names) < 2 or names == self._wd_done_names:
            return
        if self._wd_pending and names == self._wd_names:
            return
        self._wd_names = names
        self._wd_pending = True
        self._wd_generation += 1
        generation = self._wd_generation
        note = "Wikidata : recherche des interactions documentées…"
        if self._interactions_last_report:
            note = f"{self._interactions_last_report} {note}"
        self.interactions.set_status(note)

        def job(progress, fraction):
            return sb.wikidata_interactions(list(names), progress=progress)

        worker = Worker(job)
        self._workers.append(worker)
        worker.signals.finished.connect(
            lambda result, g=generation: self._interactions_wd_done(g, result)
        )
        worker.signals.failed.connect(
            lambda message, g=generation: self._interactions_wd_failed(g, message)
        )
        self.pool.start(worker)

    def _interactions_wd_done(self, generation: int, result) -> None:
        if generation != self._wd_generation:
            return
        self._wd_pending = False
        self._wd_done_names = self._wd_names
        self._wd_pairs.update(result or {})
        self._interactions_display()
        self.statusBar().showMessage(
            f"Wikidata : {len(self._wd_pairs)} paire(s) documentée(s) en cache.",
            5000,
        )

    def _interactions_wd_failed(self, generation: int, message: str) -> None:
        if generation != self._wd_generation:
            return
        self._wd_pending = False
        self._wd_done_names = self._wd_names
        self.statusBar().showMessage(
            f"Wikidata indisponible ({message}) — les sources locales restent "
            "affichées.", 10000,
        )

    def _interactions_wikidata_toggled(self, checked: bool) -> None:
        self.settings.setValue("interactions_wikidata", bool(checked))
        if checked:
            self._wd_done_names = ()      # relance volontaire
        self._interactions_refresh()
        self._interactions_display()

    def _interactions_add(self, name: str) -> None:
        name = str(name).strip()
        if not name:
            return
        target = rm.fold_accents(name).casefold()
        if any(rm.fold_accents(n).casefold() == target
               for n in self.interactions.names()):
            self.statusBar().showMessage(
                f"« {name} » est déjà dans la liste.", 5000
            )
            return
        self.interactions.add_names([name])
        self._interactions_persist()
        self._interactions_refresh()

    def _interactions_suggestions(self) -> list[str]:
        suggestions: list[str] = []
        if self.root is not None:
            for entry in sb.molecule_catalogue(self.root):
                suggestions.append(str(entry.get("name", "")))
        index = self._ddinter_index()
        if index:
            suggestions.extend(index.get("names", {}).values())
        return [s for s in suggestions if s.strip()]

    def _interactions_add_dialog(self) -> None:
        if not self._require_workspace():
            return
        name = MoleculeInputDialog.ask(self, self._interactions_suggestions())
        if name:
            self._interactions_add(name)

    def _interactions_add_from_tree(self, path) -> None:
        """Glisser-depot ou clic droit : resout la molecule puis l'ajoute."""
        if not self._require_workspace():
            return
        path = Path(path)
        if path.is_dir() and path != self.root:
            name = sb.read_molecule_meta(path) or path.name
        else:
            _directory, name = sb.locate_molecule(path, self.root)
        if not name:
            QMessageBox.information(
                self, "Pas de molécule",
                "Ce chemin n'est associé à aucune molécule. Associez d'abord "
                "le dossier à une molécule (clic droit), ou saisissez le nom "
                "avec « Ajouter une molécule… ».",
            )
            return
        self._interactions_add(name)
        self.show_page(2)

    def _interactions_remove_selected(self) -> None:
        selected = {rm.fold_accents(n).casefold()
                    for n in self.interactions.selected_names()}
        if not selected:
            return
        remaining = [n for n in self.interactions.names()
                     if rm.fold_accents(n).casefold() not in selected]
        self.interactions.set_names(remaining)
        self._interactions_persist()
        self._interactions_refresh()

    def _interactions_clear(self) -> None:
        if not self.interactions.names():
            return
        answer = QMessageBox.question(
            self, "Vider la liste",
            "Retirer toutes les molécules de la page Interactions ?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.interactions.set_names([])
        self._interactions_persist()
        self._interactions_refresh()

    def _interactions_use_compare(self) -> None:
        names = [str(row.get("name", "")) for row in self.compare.rows]
        if not names:
            names = [str(item.get("name", "")) for item in self._compare_items()]
        if not names:
            self.statusBar().showMessage(
                "La comparaison est vide : ajoutez-y d'abord des molécules.",
                5000,
            )
            return
        merged = self.interactions.names() + names
        self.interactions.set_names(merged)
        self._interactions_persist()
        self._interactions_refresh()
        self.statusBar().showMessage(
            f"{len(self.interactions.names())} molécule(s) reprises de la "
            "comparaison.", 5000,
        )

    def _interactions_download_base(self) -> None:
        if self._task is not None:
            QMessageBox.information(
                self, "Patientez", "Une opération est déjà en cours."
            )
            return

        def build(choices, progress, fraction):
            return sb.download_ddinter(progress=progress, fraction=fraction)

        def done(status):
            self._ddinter_index_cache = None
            sb.ddinter_index(use_cache=False)     # recharge l'index en memoire
            self._interactions_update_base_label()
            self._interactions_refresh()
            self.statusBar().showMessage(
                f"Base DDInter prête : {status['count']}/"
                f"{status['expected']} fichiers.", 8000,
            )

        self._start_task(
            Task("Téléchargement de la base DDInter", build, done)
        )

    def _interactions_export(self) -> None:
        rows = self.interactions.visible_rows()
        if not rows:
            self.statusBar().showMessage("Aucune paire à exporter.", 5000)
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, "Exporter les interactions", "interactions.csv",
            "Fichier CSV (*.csv)",
        )
        if not path:
            return
        try:
            sb.interaction_rows_to_csv(Path(path), rows)
        except OSError as exc:
            QMessageBox.warning(self, "Écriture impossible", str(exc))
            return
        self.statusBar().showMessage(f"Exporté : {path}", 6000)

    def save_tables(self, which: str = "both") -> None:
        """Enregistre comparaison et/ou interactions dans un fichier JSON."""
        if not self._require_workspace():
            return
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Enregistrer les tableaux",
            "pharma-tableaux.json",
            "Fichier JSON (*.json)",
        )
        if not path:
            return
        try:
            hidden = self.compare.hidden_columns() if hasattr(
                self.compare, "hidden_columns"
            ) else []
            sb.save_tables(
                Path(path),
                self.compare.rows,
                self.interactions.names(),
                root=self.root,
                hidden_columns=hidden,
                which=which,
            )
        except (OSError, sb.TablesError) as exc:
            QMessageBox.warning(self, "Enregistrement impossible", str(exc))
            return
        self.statusBar().showMessage(f"Tableaux enregistrés : {path}", 5000)

    def load_tables(self, which: str = "both") -> None:
        """Charge comparaison et/ou interactions depuis un fichier JSON."""
        if not self._require_workspace():
            return
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Charger les tableaux",
            str(self.root or Path.home()),
            "Fichier JSON (*.json)",
        )
        if not path:
            return
        try:
            data = sb.load_tables(Path(path), which=which)
        except sb.TablesError as exc:
            QMessageBox.warning(self, "Chargement impossible", str(exc))
            return
        if which in ("both", "comparaison"):
            compare_items = data.get("comparaison") or []
            hidden = data.get("colonnes_masquees") or []
            if hasattr(self.compare, "set_hidden_columns"):
                self.compare.set_hidden_columns(hidden)
            self._compare_items_cache = [
                {"name": str(item.get("name", "")), "dir": item.get("dir")}
                for item in compare_items
                if str(item.get("name", "")).strip()
            ]
            # _compare_reload() relit le cache, reconstruit les lignes depuis
            # le disque puis se repersiste lui-meme : ne pas appeler
            # _compare_persist() avant, il ecraserait la liste chargee.
            self._compare_reload()
            for row in self.compare.rows:
                if row.get("state") == "absente":
                    self._compare_enqueue(row)
            self._compare_pump()
        if which in ("both", "interactions"):
            interaction_names = data.get("interactions") or []
            self.interactions.set_names(interaction_names)
            self._interactions_persist()
            self._interactions_cache = None  # forcer la relecture coherente
            self._interactions_refresh()
        self.statusBar().showMessage(f"Tableaux chargés : {path}", 5000)


# --- point d'entree -----------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Atelier de recherche psychopharmacologique (interface "
                    "de bureau)."
    )
    parser.add_argument(
        "--workspace", metavar="DOSSIER",
        help="répertoire de travail à ouvrir au démarrage",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setOrganizationName(ORG_NAME)
    app.setApplicationName(APP_NAME)

    settings = QSettings(ORG_NAME, APP_NAME)
    root: Path | None = None
    if args.workspace:
        root = Path(args.workspace).expanduser()
    else:
        remembered = settings.value("workspace", "")
        if remembered and Path(str(remembered)).is_dir():
            root = Path(str(remembered))

    window = MainWindow(root)
    geometry = settings.value("geometry")
    if geometry is not None:
        window.restoreGeometry(geometry)
    splitter_state = settings.value("splitter")
    if splitter_state is not None:
        window.splitter.restoreState(splitter_state)

    if root is None:
        window.choose_workspace()
        if window.root is None:
            window.show()
            return app.exec()

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
