"""
PSIP GIS - prosty klient GIS dla PostGIS.
Uruchamianie: py -3.12 app/gui.py

Wymagane biblioteki:
    py -3.12 -m pip install psycopg2-binary matplotlib pillow shapely
"""
from dataclasses import dataclass
import math
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional

import psycopg2
from psycopg2 import sql
from shapely import wkb
from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString
from shapely.geometry import Point, MultiPoint, GeometryCollection

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk


DB_DEFAULTS = {
    "host": "localhost",
    "port": "5434",
    "dbname": "psip",
    "user": "psip",
    "password": "psip",
}

MAX_FEATURES = 50000

# Paleta sześciu klas zgodna z wcześniejszym stylem IOP w QGIS.
IOP_COLORS = (
    "#D73027",  # czerwony
    "#EF9A91",  # jasnoczerwony
    "#F8D8D5",  # bladoróżowy
    "#FFFFFF",  # biały - okolice zera
    "#D8EBDD",  # bladozielony
    "#91C9A0",  # zielony
)
IOP_MISSING_COLOR = "#808080"


@dataclass
class LayerInfo:
    schema: str
    table: str
    geom_column: str
    srid: int
    geom_type: str
    has_resolution: bool
    has_iop: bool

    @property
    def full_name(self):
        return f"{self.schema}.{self.table}"


@dataclass
class MapLayer:
    info: LayerInfo
    geometries: list
    resolution: Optional[int] = None
    visible: bool = True
    color: str = "#4f91b5"
    feature_colors: Optional[list] = None
    iop_min: Optional[float] = None
    iop_max: Optional[float] = None

    @property
    def display_name(self):
        suffix = f" | H3 {self.resolution}" if self.resolution else ""
        return f"{self.info.full_name}{suffix} ({len(self.geometries)} ob.)"


class PSIPGISApp:
    def __init__(self, root):
        self.root = root

        # __PSIP_DARK_THEME__
        self.root.configure(bg="#202124")
        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        dark = "#202124"
        panel = "#292a2d"
        field = "#303134"
        text_color = "#e8eaed"
        muted = "#aab2bf"
        border = "#45474d"

        self.style.configure(".", background=dark, foreground=text_color,
                             font=("Segoe UI", 9))
        self.style.configure("TFrame", background=dark)
        self.style.configure("TLabel", background=dark, foreground=text_color)
        self.style.configure("TLabelFrame", background=dark,
                             foreground=text_color, bordercolor=border)
        self.style.configure("TLabelFrame.Label", background=dark,
                             foreground=text_color)
        self.style.configure("TButton", background=panel,
                             foreground=text_color, bordercolor=border,
                             padding=(8, 5))
        self.style.map("TButton",
                       background=[("active", "#3c4043"),
                                   ("pressed", "#45494e"),
                                   ("disabled", dark)],
                       foreground=[("disabled", muted)])
        self.style.configure("TEntry", fieldbackground=field,
                             foreground=text_color, insertcolor=text_color,
                             bordercolor=border)
        self.style.configure("TCombobox", fieldbackground=field,
                             background=panel, foreground=text_color,
                             arrowcolor=text_color, bordercolor=border)
        self.style.map("TCombobox",
                       fieldbackground=[("readonly", field)],
                       foreground=[("readonly", text_color)],
                       selectbackground=[("readonly", field)],
                       selectforeground=[("readonly", text_color)])
        self.style.configure("TCheckbutton", background=dark,
                             foreground=text_color)
        self.style.map("TCheckbutton",
                       background=[("active", dark)],
                       foreground=[("active", text_color)])
        self.style.configure("TRadiobutton", background=dark,
                             foreground=text_color)
        self.style.configure("TSeparator", background=border)
        self.style.configure("TNotebook", background=dark,
                             bordercolor=border)
        self.style.configure("TNotebook.Tab", background=panel,
                             foreground=text_color, padding=(10, 5))
        self.style.map("TNotebook.Tab",
                       background=[("selected", "#3c4043"),
                                   ("active", "#35363a")],
                       foreground=[("selected", text_color)])
        self.style.configure("Treeview", background=panel,
                             fieldbackground=panel, foreground=text_color,
                             bordercolor=border, rowheight=25)
        self.style.configure("Treeview.Heading", background="#35363a",
                             foreground=text_color, relief="flat")
        self.style.map("Treeview",
                       background=[("selected", "#264f78")],
                       foreground=[("selected", "#ffffff")])
        self.style.configure("Vertical.TScrollbar",
                             background=panel, troughcolor=dark,
                             arrowcolor=text_color)
        self.style.configure("Horizontal.TScrollbar",
                             background=panel, troughcolor=dark,
                             arrowcolor=text_color)
        self.root.option_add("*Listbox.background", panel)
        self.root.option_add("*Listbox.foreground", text_color)
        self.root.option_add("*Listbox.selectBackground", "#264f78")
        self.root.option_add("*Listbox.selectForeground", "#ffffff")

        self.root.title("PSIP GIS \u2014 mened\u017cer danych przestrzennych")
        self.root.geometry("1280x800")
        self.root.minsize(900, 600)

        self.db_config = DB_DEFAULTS.copy()
        self.layers = []
        self.layer_infos = []
        self._building_layer_list = False

        self._build_menu()
        self._build_layout()
        self._set_status("Gotowy. \u0141\u0105czenie z baz\u0105 PostGIS...")
        self.root.after(250, self.connect_and_load_defaults)

    def _build_menu(self):
        menu = tk.Menu(self.root)
        database_menu = tk.Menu(menu, tearoff=False)
        database_menu.add_command(label="Po\u0142\u0105czenie z baz\u0105...", command=self.connection_dialog)
        database_menu.add_command(label="Odśwież listę danych", command=self.refresh_layer_catalog)
        menu.add_cascade(label="Baza danych", menu=database_menu)

        layer_menu = tk.Menu(menu, tearoff=False)
        layer_menu.add_command(label="Dodaj dane…", command=self.add_data_dialog)
        layer_menu.add_command(label="UsuĹ„ zaznaczonÄ… warstwę", command=self.remove_selected_layer)
        menu.add_cascade(label="Warstwy", menu=layer_menu)

        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="O programie", command=self.about)
        menu.add_cascade(label="Pomoc", menu=help_menu)
        self.root.config(menu=menu)

    def _build_layout(self):
        top = ttk.Frame(self.root, padding=(8, 7))
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="PSIP GIS", font=("Segoe UI", 15, "bold")).pack(side=tk.LEFT, padx=(0, 18))
        ttk.Button(top, text="＋ Dodaj dane", command=self.add_data_dialog).pack(side=tk.LEFT, padx=3)
        ttk.Button(top, text="Połączenie", command=self.connection_dialog).pack(side=tk.LEFT, padx=3)
        ttk.Button(top, text="Odśwież", command=self.refresh_layer_catalog).pack(side=tk.LEFT, padx=3)
        ttk.Button(top, text="Usuń", command=self.remove_selected_layer).pack(side=tk.LEFT, padx=3)
        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X)

        body = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        sidebar = ttk.Frame(body, padding=6, width=285)
        map_frame = ttk.Frame(body, padding=4)
        body.add(sidebar, weight=0)
        body.add(map_frame, weight=1)

        ttk.Label(sidebar, text="Warstwy mapy", font=("Segoe UI", 11, "bold")).pack(anchor=tk.W, pady=(0, 6))
        ttk.Label(sidebar, text="Dwuklik: pokaż/ukryj warstwę", foreground="#555").pack(anchor=tk.W, pady=(0, 5))

        list_frame = ttk.Frame(sidebar)
        list_frame.pack(fill=tk.BOTH, expand=True)
        self.layer_list = tk.Listbox(
            list_frame, selectmode=tk.SINGLE, activestyle="dotbox",
            font=("Segoe UI", 9), borderwidth=1, relief=tk.SOLID,
            exportselection=False
        )
        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.layer_list.yview)
        self.layer_list.configure(yscrollcommand=scroll.set)
        self.layer_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.layer_list.bind("<Double-Button-1>", self.toggle_selected_layer)
        self.layer_list.bind("<Button-3>", self.show_layer_context_menu)
        row = ttk.Frame(sidebar)
        row.pack(fill=tk.X, pady=(7, 0))
        ttk.Button(row, text="Pokaż/ukryj", command=self.toggle_selected_layer).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3))
        ttk.Button(row, text="Usuń", command=self.remove_selected_layer).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 0))

        self.map_title = ttk.Label(map_frame, text="Widok mapy", font=("Segoe UI", 11, "bold"))
        self.map_title.pack(anchor=tk.W, pady=(0, 4))

        self.figure = Figure(figsize=(8, 6), dpi=100, facecolor="#17191f")
        self.ax = self.figure.add_subplot(111)
        self.ax.set_position([0, 0, 1, 1])
        self.figure.subplots_adjust(left=0, right=1, bottom=0, top=1)
        self.ax.set_facecolor("#17191f")
        self.ax.set_xlabel("")
        self.ax.set_ylabel("")
        self.ax.grid(True, linewidth=0.5, color="#444851", alpha=0.65)
        self.ax.text(
            0.5, 0.5, "Dodaj dane z bazy PostGIS",
            transform=self.ax.transAxes, ha="center", va="center",
            fontsize=14, color="#aab2bf"
        )
        self.canvas = FigureCanvasTkAgg(self.figure, master=map_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        toolbar_row = ttk.Frame(map_frame)
        toolbar_row.pack(side=tk.BOTTOM, fill=tk.X)
        self.toolbar = NavigationToolbar2Tk(self.canvas, toolbar_row, pack_toolbar=False)
        self.toolbar.update()
        if hasattr(self.toolbar, "_message_label"):
            self.toolbar._message_label.pack_forget()
        self.toolbar.pack(side=tk.LEFT, anchor=tk.W)

        coord_box = ttk.LabelFrame(toolbar_row, text="Współrzędne kursora", padding=(5, 2))
        coord_box.pack(side=tk.LEFT, padx=(8, 0), pady=2)
        self.coord_var = tk.StringVar(value="D\u0142ugo\u015b\u0107: ---   Szeroko\u015b\u0107: ---")
        self.coord_label = ttk.Label(
            coord_box, textvariable=self.coord_var, width=44,
            font=("Segoe UI", 9), anchor=tk.CENTER
        )
        self.coord_label.pack()

        self.canvas.mpl_connect("motion_notify_event", self._on_map_motion)
        self.canvas.mpl_connect("button_press_event", self._on_map_pan_press)
        self.canvas.mpl_connect("button_release_event", self._on_map_pan_release)
        self.canvas.mpl_connect("motion_notify_event", self._on_map_pan_motion)
        self.canvas.mpl_connect("scroll_event", self._on_map_scroll)

        self.status_var = tk.StringVar(value="Gotowy")
        status = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, padding=(8, 4))
        status.pack(side=tk.BOTTOM, fill=tk.X)

    def _on_map_motion(self, event):
        if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
            self.coord_var.set("D\u0142ugo\u015b\u0107: ---   Szeroko\u015b\u0107: ---")
            return
        self.coord_var.set(f"D\u0142ugo\u015b\u0107: {event.xdata:.6f}   Szeroko\u015b\u0107: {event.ydata:.6f}")

    def _on_map_pan_press(self, event):
        # Przesuwanie tylko srodkowym przyciskiem myszy.
        if event.inaxes != self.ax or event.button != 2:
            return
        if event.x is None or event.y is None:
            return

        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        self._pan_start = (event.x, event.y, x0, x1, y0, y1)

    def _on_map_pan_motion(self, event):
        start = getattr(self, "_pan_start", None)
        if start is None or event.x is None or event.y is None:
            return

        px, py, x0, x1, y0, y1 = start
        bbox = self.ax.bbox
        if bbox.width <= 0 or bbox.height <= 0:
            return

        dx = (event.x - px) * (x1 - x0) / bbox.width
        dy = (event.y - py) * (y1 - y0) / bbox.height

        self.ax.set_xlim(x0 - dx, x1 - dx)
        self.ax.set_ylim(y0 - dy, y1 - dy)
        self.ax.set_aspect("auto")
        self.ax.margins(x=0.005, y=0.005)
        self.canvas.draw_idle()

    def _on_map_pan_release(self, event):
        if event.button == 2:
            self._pan_start = None

    def _on_map_scroll(self, event):
        if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
            return
        factor = 1 / 1.2 if event.button == "up" else 1.2
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        x, y = event.xdata, event.ydata
        width = (x1 - x0) * factor
        height = (y1 - y0) * factor
        rx = (x - x0) / (x1 - x0)
        ry = (y - y0) / (y1 - y0)
        self.ax.set_xlim(x - width * rx, x + width * (1 - rx))
        self.ax.set_ylim(y - height * ry, y + height * (1 - ry))
        self.ax.set_aspect("auto")
        self.ax.margins(x=0.005, y=0.005)
        self.canvas.draw_idle()

    def _set_status(self, text):
        self.status_var.set(text)
        self.root.update_idletasks()

    def _connect(self):
        cfg = self.db_config.copy()
        cfg["port"] = int(cfg["port"])
        return psycopg2.connect(connect_timeout=5, **cfg)

    def connection_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("Połączenie z PostGIS")
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)

        fields = [
            ("Host", "host"),
            ("Port", "port"),
            ("Baza danych", "dbname"),
            ("UĹĽytkownik", "user"),
            ("HasĹ‚o", "password"),
        ]
        vars_by_key = {}
        for r, (label, key) in enumerate(fields):
            ttk.Label(win, text=label).grid(row=r, column=0, padx=10, pady=6, sticky=tk.W)
            var = tk.StringVar(value=self.db_config.get(key, ""))
            entry = ttk.Entry(win, textvariable=var, width=34, show="*" if key == "password" else "")
            entry.grid(row=r, column=1, padx=10, pady=6, sticky=tk.EW)
            vars_by_key[key] = var

        def test_and_save():
            new_cfg = {key: var.get().strip() for key, var in vars_by_key.items()}
            if not new_cfg["host"] or not new_cfg["port"] or not new_cfg["dbname"] or not new_cfg["user"]:
                messagebox.showwarning("Brak danych", "Uzupełnij host, port, bazę i użytkownika.", parent=win)
                return
            try:
                old_cfg = self.db_config
                self.db_config = new_cfg
                conn = self._connect()
                with conn.cursor() as cur:
                    cur.execute("SELECT PostGIS_Full_Version()")
                    version = cur.fetchone()[0]
                conn.close()
            except Exception as exc:
                self.db_config = old_cfg if "old_cfg" in locals() else self.db_config
                messagebox.showerror("Błąd poĹ‚Ä…czenia", str(exc), parent=win)
                return
            win.destroy()
            self._set_status("PoĹ‚Ä…czono z PostGIS.")
            messagebox.showinfo("Po\u0142\u0105czenie dzia\u0142a", "Po\u0142\u0105czono z baz\u0105 psip.\\n\\n" + version.splitlines()[0], parent=self.root)
            self.refresh_layer_catalog()
            self.load_default_layers()

        buttons = ttk.Frame(win)
        buttons.grid(row=len(fields), column=0, columnspan=2, padx=10, pady=10, sticky=tk.E)
        ttk.Button(buttons, text="Testuj i zapisz", command=test_and_save).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="Anuluj", command=win.destroy).pack(side=tk.LEFT, padx=4)
        win.columnconfigure(1, weight=1)

    def connect_and_load_defaults(self):
        try:
            conn = self._connect()
            conn.close()
            self.refresh_layer_catalog(show_errors=False)
            self.load_default_layers()
        except Exception as exc:
            self._set_status("Brak poĹ‚Ä…czenia. Ustaw połączenie z bazą.")
            messagebox.showwarning(
                "Nie udało się poĹ‚Ä…czyć z PostGIS",
                "Sprawdź, czy kontener bazy działa i czy dane poĹ‚Ä…czenia są poprawne.\n\n"
                f"SzczegĂłĹ‚y: {exc}\n\n"
                "Możesz zmienić ustawienia w menu „Baza danych â†’ Połączenie z bazą”.",
                parent=self.root
            )

    def refresh_layer_catalog(self, show_errors=True):
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT gc.f_table_schema, gc.f_table_name,
                               gc.f_geometry_column, gc.srid, UPPER(gc.type),
                               EXISTS (
                                   SELECT 1
                                   FROM information_schema.columns c
                                   WHERE c.table_schema = gc.f_table_schema
                                     AND c.table_name = gc.f_table_name
                                     AND c.column_name = 'resolution'
                               ) AS has_resolution,
                                (
                                    gc.f_table_schema = 'grid'
                                    AND gc.f_table_name IN (
                                        'iop_color_6', 'iop_color_7', 'iop_color_8'
                                    )
                                    AND EXISTS (
                                        SELECT 1
                                        FROM information_schema.columns c
                                        WHERE c.table_schema = gc.f_table_schema
                                          AND c.table_name = gc.f_table_name
                                          AND c.column_name = 'iop'
                                    )
                                ) AS has_iop
                        FROM public.geometry_columns gc
                        WHERE gc.f_table_schema IN ('public', 'grid', 'bdot')
                        ORDER BY
                            CASE gc.f_table_schema
                                WHEN 'grid' THEN 1
                                WHEN 'bdot' THEN 2
                                ELSE 3
                            END,
                            gc.f_table_name
                    """)
                    rows = cur.fetchall()
            self.layer_infos = [LayerInfo(*row) for row in rows]
            self._set_status(f"Po\u0142\u0105czono. Znaleziono {len(self.layer_infos)} tabel przestrzennych.")
            return True
        except Exception as exc:
            if show_errors:
                messagebox.showerror("Błąd", f"Nie udało się pobraÄ‡ listy warstw:\n{exc}", parent=self.root)
            self._set_status("Nie udało się pobraÄ‡ listy warstw.")
            return False

    def load_default_layers(self):
        if not self.layer_infos:
            return
        defaults = [
            ("public", "powiat", None),
            ("grid", "hexagons", 7),
        ]
        added = 0
        for schema, table, resolution in defaults:
            if any(layer.info.schema == schema and layer.info.table == table and layer.resolution == resolution for layer in self.layers):
                continue
            info = next((x for x in self.layer_infos if x.schema == schema and x.table == table), None)
            if info:
                try:
                    self._load_layer(info, resolution, fit=True, show_errors=False)
                    added += 1
                except Exception:
                    pass
        if added:
            self._refresh_layer_list()
            self.draw_map(fit=True)
        elif not self.layers:
            self._set_status("Po\u0142\u0105czono. Kliknij \u201eDodaj dane\u201d, aby wczyta\u0107 warstwy.")

    def add_data_dialog(self):
        if not self.refresh_layer_catalog(show_errors=False):
            messagebox.showerror(
                "Brak połączenia",
                "Najpierw skonfiguruj połączenie z bazą PostGIS.",
                parent=self.root
            )
            return

        win = tk.Toplevel(self.root)
        win.title("Dodaj dane z PostGIS")
        win.geometry("850x520")
        win.minsize(700, 380)

        top = ttk.Frame(win, padding=10)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Szukaj warstwy:").pack(side=tk.LEFT)
        search_var = tk.StringVar()
        search_entry = ttk.Entry(top, textvariable=search_var, width=35)
        search_entry.pack(side=tk.LEFT, padx=8)

        columns = ("schema", "table", "type", "srid", "resolution")
        tree = ttk.Treeview(
            win, columns=columns, show="headings", selectmode="browse"
        )

        tree.heading("schema", text="Schemat")
        tree.heading("table", text="Tabela")
        tree.heading("type", text="Typ geometrii")
        tree.heading("srid", text="EPSG")
        tree.heading("resolution", text="Rozdzielczość")

        tree.column("schema", width=100)
        tree.column("table", width=230)
        tree.column("type", width=160)
        tree.column("srid", width=75, anchor=tk.CENTER)
        tree.column("resolution", width=120, anchor=tk.CENTER)

        tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

        bottom = ttk.Frame(win, padding=10)
        bottom.pack(fill=tk.X)
        ttk.Label(
            bottom,
            text="Wybierz konkretną warstwę z listy."
        ).pack(side=tk.LEFT)

        row_map = {}
        resolution_cache = {}

        def populate(*_):
            tree.delete(*tree.get_children())
            row_map.clear()
            needle = search_var.get().strip().lower()

            for i, info in enumerate(self.layer_infos):
                if info.has_resolution:
                    if i not in resolution_cache:
                        conn = self._connect()
                        try:
                            with conn.cursor() as cur:
                                cur.execute(
                                    sql.SQL("""
                                        SELECT DISTINCT resolution
                                        FROM {}.{}
                                        WHERE resolution IS NOT NULL
                                        ORDER BY resolution
                                    """).format(
                                        sql.Identifier(info.schema),
                                        sql.Identifier(info.table)
                                    )
                                )
                                resolution_cache[i] = [
                                    int(row[0]) for row in cur.fetchall()
                                ]
                        except Exception as exc:
                            messagebox.showerror(
                                "Błąd odczytu rozdzielczości",
                                f"{info.full_name}: {exc}",
                                parent=win
                            )
                            return
                        finally:
                            conn.close()

                    variants = resolution_cache[i] or [None]
                else:
                    variants = [None]

                for resolution in variants:
                    table_label = (
                        f"{info.table} {resolution}"
                        if resolution is not None
                        else info.table
                    )

                    searchable = (
                        f"{info.schema} {table_label} "
                        f"{info.geom_type} {info.srid}"
                    ).lower()

                    if needle and needle not in searchable:
                        continue

                    iid = f"{i}:{resolution if resolution is not None else 'all'}"
                    tree.insert(
                        "", tk.END, iid=iid,
                        values=(
                            info.schema,
                            table_label,
                            info.geom_type,
                            info.srid,
                            resolution if resolution is not None else "?"
                        )
                    )
                    row_map[iid] = (info, resolution)

        def add_selected():
            selected = tree.selection()
            if not selected:
                messagebox.showwarning(
                    "Wybierz warstwę",
                    "Zaznacz wiersz, który chcesz dodać.",
                    parent=win
                )
                return

            info, resolution = row_map[selected[0]]
            try:
                self._load_layer(info, resolution, fit=True)
                win.destroy()
            except Exception as exc:
                messagebox.showerror(
                    "Nie udało się wczytać warstwy",
                    str(exc),
                    parent=win
                )

        tree.bind("<Double-1>", lambda event: add_selected())
        search_var.trace_add("write", populate)

        ttk.Button(
            bottom, text="Anuluj", command=win.destroy
        ).pack(side=tk.RIGHT, padx=(5, 0))
        ttk.Button(
            bottom, text="Dodaj", command=add_selected
        ).pack(side=tk.RIGHT)

        populate()
        search_entry.focus_set()

    @staticmethod
    def _iop_color(value, minimum, maximum):
        """
        Przypisuje kolor do pojedynczego heksagonu.
        Przedziały są liczone osobno dla każdej warstwy H3,
        tak jak w poprzednim stylu QGIS.
        """
        if value is None or minimum is None or maximum is None:
            return IOP_MISSING_COLOR

        if math.isclose(minimum, maximum):
            return IOP_COLORS[3]

        if minimum < 0 < maximum:
            # Podział zgodny z wcześniejszym stylem QGIS:
            # trzy klasy po stronie ujemnej i trzy po dodatniej.
            bounds = [
                minimum,
                minimum * 2.0 / 3.0,
                minimum / 3.0,
                0.0,
                maximum / 3.0,
                maximum * 2.0 / 3.0,
                maximum,
            ]
        else:
            # Zabezpieczenie dla warstwy, której zakres nie obejmuje zera.
            step = (maximum - minimum) / 6.0
            bounds = [minimum + step * i for i in range(7)]

        for index in range(6):
            # Wartość dokładnie równa 0 trafia do klasy białej.
            if value < bounds[index + 1] or index == 5:
                return IOP_COLORS[index]

        return IOP_COLORS[-1]

    def _load_layer(self, info, resolution=None, fit=True, show_errors=True):
        self._set_status(f"Wczytywanie {info.full_name}…")

        geom_ident = sql.Identifier(info.geom_column)
        iop_select = (
            sql.SQL(", {}").format(sql.Identifier("iop"))
            if info.has_iop else sql.SQL("")
        )

        query = sql.SQL("""
            SELECT
                ST_AsBinary(ST_Transform({geom}, 4326))
                {iop_select}
            FROM {schema}.{table}
            WHERE {geom} IS NOT NULL
        """).format(
            geom=geom_ident,
            iop_select=iop_select,
            schema=sql.Identifier(info.schema),
            table=sql.Identifier(info.table),
        )

        if resolution is not None and info.has_resolution:
            query += sql.SQL(" AND resolution = %s")
        query += sql.SQL(" LIMIT %s")

        params = []
        if resolution is not None and info.has_resolution:
            params.append(resolution)
        params.append(MAX_FEATURES)

        geometries = []
        feature_values = []
        iop_min = None
        iop_max = None
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                for row in cur.fetchall():
                    geom_bin = row[0]
                    if not geom_bin:
                        continue

                    geometries.append(wkb.loads(bytes(geom_bin)))

                    if info.has_iop:
                        raw_value = row[1]
                        feature_values.append(
                            float(raw_value) if raw_value is not None else None
                        )

                # Zakres globalny z całej tabeli źródłowej grid.iop_test.
                # To odtwarza wcześniejszy styl QGIS, w którym minimum i maksimum
                # były pobierane przed filtrowaniem warstw na rozdzielczości.
                if info.has_iop:
                    cur.execute(
                        "SELECT MIN(iop), MAX(iop) FROM grid.iop_test "
                        "WHERE iop IS NOT NULL"
                    )
                    range_row = cur.fetchone()
                    if range_row and range_row[0] is not None and range_row[1] is not None:
                        iop_min = float(range_row[0])
                        iop_max = float(range_row[1])
                    else:
                        iop_min = None
                        iop_max = None
        finally:
            conn.close()

        if not geometries:
            raise RuntimeError("Warstwa nie zawiera geometrii do wyświetlenia.")

        feature_colors = None

        if info.has_iop:
            feature_colors = [
                self._iop_color(value, iop_min, iop_max)
                for value in feature_values
            ]

        if info.schema == "grid":
            color = "#4f91b5"
        elif info.geom_type in ("LINESTRING", "MULTILINESTRING"):
            color = "#b47b42"
        elif info.geom_type in ("POINT", "MULTIPOINT"):
            color = "#b64646"
        elif info.table == "powiat":
            color = "#303f50"
        else:
            color = "#6c9b72"

        # Zastąp istniejącą warstwę o tym samym źródle i tej samej rozdzielczości.
        self.layers = [
            layer for layer in self.layers
            if not (
                layer.info.full_name == info.full_name
                and layer.resolution == resolution
            )
        ]

        self.layers.append(
            MapLayer(
                info=info,
                geometries=geometries,
                resolution=resolution,
                visible=True,
                color=color,
                feature_colors=feature_colors,
                iop_min=iop_min,
                iop_max=iop_max,
            )
        )

        self._refresh_layer_list()
        self.draw_map(fit=fit)

        status_text = (
            f"Wczytano {info.full_name}: {len(geometries)} obiektów."
            + (f" (limit {MAX_FEATURES})" if len(geometries) == MAX_FEATURES else "")
        )
        if info.has_iop and iop_min is not None and iop_max is not None:
            status_text += f" Zakres IOP: {iop_min:.4f} do {iop_max:.4f}."
        self._set_status(status_text)

    def _refresh_layer_list(self):
        self.layer_list.delete(0, tk.END)
        for layer in self.layers:
            marker = "\u25cf" if layer.visible else "\u25cb"
            self.layer_list.insert(tk.END, f"{marker}  {layer.display_name}")

    def _selected_layer_index(self):
        selection = self.layer_list.curselection()
        return selection[0] if selection else None


    def show_layer_context_menu(self, event):
        idx = self.layer_list.nearest(event.y)
        if idx < 0 or idx >= len(self.layers):
            return "break"

        self.layer_list.selection_clear(0, tk.END)
        self.layer_list.selection_set(idx)
        self.layer_list.activate(idx)

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(
            label="Przybli\u017c do warstwy",
            command=self.zoom_to_selected_layer
        )
        menu.add_command(
            label="W\u0142a\u015bciwo\u015bci",
            command=self.show_selected_layer_properties
        )

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

        return "break"

    def zoom_to_selected_layer(self):
        idx = self._selected_layer_index()
        if idx is None or idx >= len(self.layers):
            return

        layer = self.layers[idx]
        bounds = [
            geom.bounds for geom in layer.geometries
            if geom is not None and not geom.is_empty
        ]

        if not bounds:
            self._set_status("Warstwa nie zawiera geometrii.")
            return

        minx = min(b[0] for b in bounds)
        miny = min(b[1] for b in bounds)
        maxx = max(b[2] for b in bounds)
        maxy = max(b[3] for b in bounds)

        dx = maxx - minx
        dy = maxy - miny
        pad = max(dx, dy, 1.0) * 0.005

        self.ax.set_xlim(minx - pad, maxx + pad)
        self.ax.set_ylim(miny - pad, maxy + pad)
        self.ax.set_aspect("auto")
        self.ax.margins(x=0.005, y=0.005)
        self.canvas.draw_idle()
        self._set_status(
            f"Przybli\u017cono do warstwy: {layer.display_name}."
        )

    def show_selected_layer_properties(self):
        idx = self._selected_layer_index()
        if idx is None or idx >= len(self.layers):
            return

        layer = self.layers[idx]
        info = layer.info

        def attr(obj, *names, default="\u2014"):
            for name in names:
                value = getattr(obj, name, None)
                if value is not None:
                    return value
            return default

        details = [
            ("Nazwa", attr(layer, "display_name")),
            ("Schemat", attr(info, "schema")),
            ("Tabela", attr(info, "table")),
            ("Kolumna geometrii",
             attr(info, "geom_column", "geometry_column")),
            ("Typ geometrii",
             attr(info, "geom_type", "geometry_type", "type")),
            ("Uk\u0142ad wsp\u00f3\u0142rz\u0119dnych (SRID)",
             attr(info, "srid", "srid_id")),
            ("Rozdzielczo\u015b\u0107 H3",
             attr(layer, "resolution")),
            ("Liczba obiekt\u00f3w", len(layer.geometries)),
            ("Widoczno\u015b\u0107",
             "Tak" if layer.visible else "Nie"),
        ]

        if layer.feature_colors is not None:
            details.append((
                "Zakres IOP",
                (
                    f"{layer.iop_min:.4f} – {layer.iop_max:.4f}"
                    if layer.iop_min is not None and layer.iop_max is not None
                    else "brak wartości"
                ),
            ))

        message = "\n".join(
            f"{label}: {value}" for label, value in details
        )
        messagebox.showinfo(
            "W\u0142a\u015bciwo\u015bci warstwy",
            message,
            parent=self.root
        )

    def toggle_selected_layer(self, _event=None):
        idx = self._selected_layer_index()
        if idx is None:
            return
        self.layers[idx].visible = not self.layers[idx].visible
        self._refresh_layer_list()
        self.layer_list.selection_set(idx)
        self.draw_map(fit=False)

    def remove_selected_layer(self):
        idx = self._selected_layer_index()
        if idx is None:
            messagebox.showinfo("Warstwy", "Najpierw zaznacz warstwę na liĹ›cie.", parent=self.root)
            return
        removed = self.layers.pop(idx)
        self._refresh_layer_list()
        self.draw_map(fit=True)
        self._set_status(f"UsuniÄ™to z widoku: {removed.info.full_name}. Dane w bazie nie zostaĹ‚y zmienione.")

    def draw_map(self, fit=False):
        old_xlim = self.ax.get_xlim()
        old_ylim = self.ax.get_ylim()
        had_extent = (
            len(old_xlim) == 2 and len(old_ylim) == 2
            and old_xlim[0] != old_xlim[1] and old_ylim[0] != old_ylim[1]
            and not fit
        )

        self.ax.clear()
        self.ax.set_axis_off()
        self.ax.set_position([0, 0, 1, 1])
        self.ax.tick_params(axis="both", which="both", labelbottom=False, labelleft=False, bottom=False, left=False)
        self.ax.set_facecolor("#17191f")
        visible_layers = [layer for layer in self.layers if layer.visible]
        if not visible_layers:
            self.ax.text(0.5, 0.5, "Brak widocznych warstw\nKliknij „Dodaj dane”",
                         transform=self.ax.transAxes, ha="center", va="center",
                         fontsize=14, color="#aab2bf")
            self.ax.set_xlabel("")
            self.ax.set_ylabel("")
            self.ax.grid(True, linewidth=0.5, color="#444851", alpha=0.65)
            self.ax.set_aspect("auto")
            self.ax.margins(x=0.005, y=0.005)
            self.canvas.draw_idle()
            return

        for layer in visible_layers:
            for feature_index, geom in enumerate(layer.geometries):
                feature_color = (
                    layer.feature_colors[feature_index]
                    if layer.feature_colors is not None
                    and feature_index < len(layer.feature_colors)
                    else None
                )
                self._draw_geometry(geom, layer, feature_color)

        self.ax.set_xlabel("")
        self.ax.set_ylabel("")
        self.ax.grid(True, linewidth=0.5, color="#444851", alpha=0.65)
        self.ax.ticklabel_format(useOffset=False)
        self.ax.set_aspect("auto")

        if had_extent:
            self.ax.set_xlim(old_xlim)
            self.ax.set_ylim(old_ylim)
        else:
            bounds = [g.bounds for layer in visible_layers for g in layer.geometries if not g.is_empty]
            if bounds:
                minx = min(b[0] for b in bounds)
                miny = min(b[1] for b in bounds)
                maxx = max(b[2] for b in bounds)
                maxy = max(b[3] for b in bounds)
                dx = max(maxx - minx, 0.0001)
                dy = max(maxy - miny, 0.0001)
                pad_x = dx * 0.001
                pad_y = dy * 0.001
                self.ax.set_xlim(minx - pad_x, maxx + pad_x)
                self.ax.set_ylim(miny - pad_y, maxy + pad_y)

        self.ax.set_position([0, 0, 1, 1])
        self.ax.set_aspect("auto")
        self.ax.margins(x=0.005, y=0.005)
        self.canvas.draw_idle()

    def _draw_geometry(self, geom, layer, feature_color=None):
        if geom is None or geom.is_empty:
            return

        draw_color = feature_color or layer.color

        if isinstance(geom, Polygon):
            x, y = geom.exterior.xy

            if layer.info.table == "powiat":
                self.ax.plot(x, y, color=draw_color, linewidth=2.0, zorder=5)
                for ring in geom.interiors:
                    ix, iy = ring.xy
                    self.ax.plot(ix, iy, color=draw_color, linewidth=1.0, zorder=5)

            elif feature_color is not None:
                # Warstwa IOP: mocne wypełnienie osobnym kolorem każdego heksagonu.
                self.ax.fill(
                    x, y,
                    facecolor=feature_color,
                    edgecolor="#45474d",
                    linewidth=0.45,
                    alpha=0.92,
                    zorder=4,
                )
                for ring in geom.interiors:
                    ix, iy = ring.xy
                    self.ax.fill(
                        ix, iy,
                        facecolor="#17191f",
                        edgecolor="#45474d",
                        linewidth=0.35,
                        zorder=4,
                    )

            else:
                # Dotychczasowy wygląd zwykłych warstw pozostaje bez zmian.
                self.ax.fill(
                    x, y,
                    facecolor=layer.color,
                    edgecolor=layer.color,
                    linewidth=0.65,
                    alpha=0.20,
                    zorder=2,
                )
                for ring in geom.interiors:
                    ix, iy = ring.xy
                    self.ax.fill(
                        ix, iy,
                        facecolor="#17191f",
                        edgecolor=layer.color,
                        linewidth=0.35,
                        zorder=2,
                    )

        elif isinstance(geom, MultiPolygon):
            for part in geom.geoms:
                self._draw_geometry(part, layer, feature_color)

        elif isinstance(geom, (LineString, MultiLineString)):
            if isinstance(geom, LineString):
                x, y = geom.xy
                self.ax.plot(
                    x, y, color=draw_color,
                    linewidth=0.8, alpha=0.9, zorder=3
                )
            else:
                for part in geom.geoms:
                    self._draw_geometry(part, layer, feature_color)

        elif isinstance(geom, Point):
            self.ax.scatter([geom.x], [geom.y], s=9, color=draw_color, zorder=4)

        elif isinstance(geom, MultiPoint):
            self.ax.scatter(
                [point.x for point in geom.geoms],
                [point.y for point in geom.geoms],
                s=9, color=draw_color, zorder=4
            )

        elif isinstance(geom, GeometryCollection):
            for part in geom.geoms:
                self._draw_geometry(part, layer, feature_color)

    def about(self):
        messagebox.showinfo(
            "O programie",
            "PSIP GIS â€” demonstracyjny klient GIS w Tkinterze.\n"
            "Dane przestrzenne s\u0105 odczytywane z PostGIS; aplikacja nie modyfikuje tabel \u017ar\u00f3d\u0142owych.\\n"
            f"Limit odczytu na jedn\u0105 warstw\u0119: {MAX_FEATURES} obiekt\u00f3w.",
            parent=self.root
        )


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    PSIPGISApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
