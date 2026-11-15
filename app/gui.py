"""
PSIP GIS - prosty klient GIS dla PostGIS.
Uruchamianie: py -3.12 app/gui.py

Wymagane biblioteki:
    py -3.12 -m pip install psycopg2-binary matplotlib pillow shapely
"""
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import secrets
import math
import os
from pathlib import Path
import re
import tempfile
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from typing import Optional

import psycopg2
from psycopg2 import sql
from shapely import force_2d, wkb
from shapely.geometry import Polygon, MultiPolygon, LineString, MultiLineString
from shapely.geometry import Point, MultiPoint, GeometryCollection, shape
from shapely.geometry.polygon import orient
from shapely.ops import transform

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

IOP_RED = (215, 48, 39)
IOP_WHITE = (255, 255, 255)
IOP_GREEN = (26, 152, 80)
IOP_MISSING_COLOR = "#bdbdbd"


THEMES = {
    "dark": {
        "bg": "#202124", "panel": "#292a2d", "field": "#303134",
        "text": "#e8eaed", "muted": "#aab2bf", "border": "#45474d",
        "hover": "#3c4043", "pressed": "#45494e", "heading": "#35363a",
        "selected": "#264f78", "accent": "#8ab4f8", "accent_bg": "#35465a",
        "map": "#17191f", "grid": "#444851", "error": "#ff9e91",
    },
    "light": {
        "bg": "#f2f4f6", "panel": "#e5eaf0", "field": "#ffffff",
        "text": "#20262e", "muted": "#4b5663", "border": "#aab4bf",
        "hover": "#d6e2ef", "pressed": "#c4d6e9", "heading": "#dce3ec",
        "selected": "#255f96", "accent": "#164b82", "accent_bg": "#d2e5f8",
        "map": "#fafbfd", "grid": "#b3bdc8", "error": "#a32020",
    },
}


class PSIPToolbar(NavigationToolbar2Tk):
    toolitems = tuple(
        item for item in NavigationToolbar2Tk.toolitems
        if item[0] not in ("Zoom", "Subplots")
    )

    def __init__(self, canvas, window, app, **kwargs):
        self.app = app
        super().__init__(canvas, window, **kwargs)
        self.full_extent_button = tk.Button(self, command=self.zoom_full_extent, takefocus=False)
        save_button = self._buttons.get("Save")
        options = {"before": save_button} if save_button is not None else {}
        self.full_extent_button.pack(side=tk.LEFT, padx=3, pady=3, **options)
        self.apply_theme(app.palette)

    def apply_theme(self, palette):
        self.BAR = palette["panel"]
        self.BUTTON = palette["field"]
        self.HOVER = palette["hover"]
        self.TEXT = palette["text"]
        self.ACCENT = palette["accent"]
        self.accent_bg = palette["accent_bg"]
        self.configure(
            bg=self.BAR,
            bd=0,
            highlightthickness=0
        )

        # Tło wszystkich elementów paska
        for child in self.winfo_children():
            if isinstance(child, tk.Frame):
                child.configure(
                    bg=self.BAR,
                    bd=0,
                    highlightthickness=0
                )

        # Ikony standardowych przycisków
        icons = {
            "Home": "⌂",
            "Back": "‹",
            "Forward": "›",
            "Pan": "✥",
            "Save": "▣"
        }

        for name, symbol in icons.items():
            button = self._buttons.get(name)

            if button is None:
                continue

            if name == "Save":
                from PIL import Image, ImageDraw, ImageTk

                save_image = Image.new(
                    "RGBA", (48, 48), (0, 0, 0, 0)
                )
                draw = ImageDraw.Draw(save_image)
                color = self.TEXT

                draw.line([(13, 15), (13, 5), (35, 5), (35, 15)], fill=color, width=3)
                draw.rounded_rectangle((5, 15, 43, 35), radius=4, outline=color, width=3)
                draw.rectangle((13, 28, 35, 43), fill=(0, 0, 0, 0), outline=color, width=3)
                draw.line([(18, 35), (30, 35)], fill=color, width=2)
                draw.ellipse((34, 20, 38, 24), fill=color)

                self._save_icon = ImageTk.PhotoImage(
                    save_image.resize(
                        (24, 24),
                        Image.Resampling.LANCZOS
                    )
                )

                button.configure(
                    image=self._save_icon,
                    text="",
                    compound=tk.CENTER,
                    bg=self.BUTTON,
                    activebackground=self.HOVER,
                    relief=tk.FLAT,
                    overrelief=tk.FLAT,
                    bd=0,
                    highlightthickness=0,
                    cursor="hand2",
                    width=30,
                    height=30,
                    padx=2,
                    pady=2
                )
            else:
                self._style_button(button, symbol)

        self._style_button(self.full_extent_button, "⛶", accent=True)

    def _style_button(self, button, symbol, accent=False):
        button.configure(
            image="",
            text=symbol,
            compound=tk.CENTER,
            font=("Segoe UI Symbol", 15),
            width=3,
            height=1,
            bg=self.accent_bg if accent else self.BUTTON,
            fg=self.ACCENT if accent else self.TEXT,
            activebackground=self.HOVER,
            activeforeground=self.ACCENT,
            relief=tk.FLAT,
            overrelief=tk.FLAT,
            bd=0,
            highlightthickness=0,
            padx=7,
            pady=4,
            cursor="hand2"
        )

    def zoom_full_extent(self):
        self.app.zoom_to_full_extent()

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

    iop_values: Optional[list] = None
    source_path: Optional[Path] = None
    editable_points: bool = False
    attribute_types: Optional[dict] = None
    attribute_rows: Optional[list] = None

    @property
    def display_name(self):
        suffix = f" | H3 {self.resolution}" if self.resolution else ""
        name = self.info.table if self.editable_points else self.info.full_name
        if self.source_path and not self.editable_points:
            name = self.source_path.stem
        return f"{name}{suffix} ({len(self.geometries)} ob.)"


class PSIPGISApp:
    def __init__(self, root, on_logout=None):
        self.root = root
        self.on_logout = on_logout

        self._theme_path = settings_directory() / "theme.txt"
        try:
            theme = self._theme_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            theme = "dark"
        self.theme_var = tk.StringVar(master=root, value=theme if theme in THEMES else "dark")
        self.palette = THEMES[self.theme_var.get()]
        self.style = ttk.Style(root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self._configure_theme()

        self.root.title("PSIP GIS \u2014 mened\u017cer danych przestrzennych")
        self.root.geometry("1280x800")
        self.root.minsize(900, 600)

        self.db_config = DB_DEFAULTS.copy()
        self.layers = []
        self.attribute_highlight = None
        self._point_target = None
        self.layer_infos = []
        self._building_layer_list = False

        self._build_menu()
        self._build_layout()
        self._set_status("Gotowy. \u0141\u0105czenie z baz\u0105 PostGIS...")
        self.root.after(250, self.connect_and_load_defaults)

    def _configure_theme(self):
        palette = self.palette
        dark, panel, field = (palette[key] for key in ("bg", "panel", "field"))
        text_color, muted, border = (palette[key] for key in ("text", "muted", "border"))
        self.root.configure(bg=dark)
        self.style.configure("TMenubutton", background=panel, foreground=text_color, arrowcolor=text_color)
        self.style.configure("Hint.TLabel", foreground=muted)
        self.style.configure("Error.TLabel", foreground=palette["error"])
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
                       background=[("active", palette["hover"]),
                                   ("pressed", palette["pressed"]),
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
                       background=[("selected", palette["hover"]),
                                   ("active", palette["heading"])],
                       foreground=[("selected", text_color)])
        self.style.configure("Treeview", background=panel,
                             fieldbackground=panel, foreground=text_color,
                             bordercolor=border, rowheight=23)
        self.style.configure("Treeview.Heading", background=palette["heading"],
                             foreground=text_color, relief="flat")
        self.style.map("Treeview",
                       background=[("selected", palette["selected"])],
                       foreground=[("selected", "#ffffff")])
        self.style.configure("Vertical.TScrollbar",
                             background=panel, troughcolor=dark,
                             arrowcolor=text_color)
        self.style.configure("Horizontal.TScrollbar",
                             background=panel, troughcolor=dark,
                             arrowcolor=text_color)
        self.root.option_add("*Listbox.background", panel)
        self.root.option_add("*Listbox.foreground", text_color)
        self.root.option_add("*Listbox.selectBackground", palette["selected"])
        self.root.option_add("*Listbox.selectForeground", "#ffffff")

    def _change_theme(self):
        self.palette = THEMES[self.theme_var.get()]
        self._configure_theme()
        self.toolbar.apply_theme(self.palette)
        self.layer_list.tag_configure("legend", foreground=self.palette["muted"])
        self.figure.set_facecolor(self.palette["map"])
        self.draw_map()
        try:
            self._theme_path.parent.mkdir(parents=True, exist_ok=True)
            self._theme_path.write_text(self.theme_var.get(), encoding="utf-8")
        except OSError:
            self._set_status("Zmieniono motyw. Nie udało się zapisać ustawienia.")

    def _build_menu(self):
        menu = tk.Menu(self.root)
        database_menu = tk.Menu(menu, tearoff=False)
        database_menu.add_command(label="Po\u0142\u0105czenie z baz\u0105...", command=self.connection_dialog)
        database_menu.add_command(label="Odśwież listę danych", command=self.refresh_layer_catalog)
        menu.add_cascade(label="Baza danych", menu=database_menu)

        layer_menu = tk.Menu(menu, tearoff=False)
        sources = tk.Menu(layer_menu, tearoff=False)
        self._add_layer_sources(sources)
        layer_menu.add_cascade(label="Dodaj warstwę", menu=sources)
        layer_menu.add_command(label="Usuń zaznaczoną warstwę", command=self.remove_selected_layer)
        menu.add_cascade(label="Warstwy", menu=layer_menu)

        view_menu = tk.Menu(menu, tearoff=False)
        themes = tk.Menu(view_menu, tearoff=False)
        for value, label in (("light", "Jasny"), ("dark", "Ciemny")):
            themes.add_radiobutton(label=label, value=value, variable=self.theme_var, command=self._change_theme)
        view_menu.add_cascade(label="Motyw", menu=themes)
        menu.add_cascade(label="Widok", menu=view_menu)

        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="O programie", command=self.about)
        menu.add_cascade(label="Pomoc", menu=help_menu)
        if self.on_logout is not None:
            account_menu = tk.Menu(menu, tearoff=False)
            account_menu.add_command(label="Wyloguj", command=self._logout)
            menu.add_cascade(label="Konto", menu=account_menu)

        self.root.config(menu=menu)

    def _logout(self):
        if any(layer.editable_points and layer.source_path is None and layer.geometries for layer in self.layers):
            if not messagebox.askyesno(
                "Wyloguj", "Tymczasowe warstwy punktowe zostaną zamknięte. Wylogować?",
                parent=self.root,
            ):
                return
        self.on_logout()

    def _build_layout(self):
        top = ttk.Frame(self.root, padding=(8, 7))
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="PSIP GIS", font=("Segoe UI", 15, "bold")).pack(side=tk.LEFT, padx=(0, 18))
        add_layer = ttk.Menubutton(top, text="＋ Dodaj warstwę")
        sources = tk.Menu(add_layer, tearoff=False)
        self._add_layer_sources(sources)
        add_layer.configure(menu=sources)
        add_layer.pack(side=tk.LEFT, padx=3)
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

        layer_header = ttk.Frame(sidebar)
        layer_header.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(layer_header, text="Warstwy mapy", font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)
        ttk.Button(layer_header, text="↓", width=3, command=lambda: self.move_selected_layer(1)).pack(side=tk.RIGHT)
        ttk.Button(layer_header, text="↑", width=3, command=lambda: self.move_selected_layer(-1)).pack(side=tk.RIGHT, padx=(0, 3))
        ttk.Label(sidebar, text="Dwuklik: pokaż/ukryj warstwę", style="Hint.TLabel").pack(anchor=tk.W, pady=(0, 5))

        self.iop_color_scale = tk.StringVar(master=self.root, value="reference")

        list_frame = ttk.Frame(sidebar)
        list_frame.pack(fill=tk.BOTH, expand=True)

        self.layer_list = ttk.Treeview(
            list_frame,
            show="tree",
            selectmode="browse",
            style="Treeview"
        )
        self.layer_list.column("#0", width=255, minwidth=180, stretch=True)

        scroll = ttk.Scrollbar(
            list_frame,
            orient=tk.VERTICAL,
            command=self.layer_list.yview
        )
        self.layer_list.configure(yscrollcommand=scroll.set)
        self.layer_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.layer_list.tag_configure("legend", foreground=self.palette["muted"])
        self.layer_list.bind(
            "<Double-Button-1>", self._on_layer_tree_double_click
        )
        self.layer_list.bind(
            "<Button-3>", self.show_layer_context_menu
        )

        self._layer_drag = None
        self.layer_list.bind("<ButtonPress-1>", self._start_layer_drag)
        self.layer_list.bind("<B1-Motion>", self._drag_layer)
        self.layer_list.bind("<ButtonRelease-1>", self._drop_layer)

        row = ttk.Frame(sidebar)
        row.pack(fill=tk.X, pady=(7, 0))

        ttk.Button(
            row, text="Pokaż/ukryj",
            command=self.toggle_selected_layer
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))

        ttk.Button(
            row, text="Przybliż do",
            command=self.zoom_to_selected_layer
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)

        ttk.Button(
            row, text="Usuń",
            command=self.remove_selected_layer
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 0))

        ttk.Button(
            sidebar, text="Szukaj po atrybucie", command=self.show_attribute_search
        ).pack(fill=tk.X, pady=(5, 0))

        from matplotlib.backends._backend_tk import add_tooltip

        map_header = ttk.Frame(map_frame)
        map_header.pack(fill=tk.X, pady=(0, 4))
        self.map_title = ttk.Label(map_header, text="Widok mapy", font=("Segoe UI", 11, "bold"))
        self.map_title.pack(side=tk.LEFT)
        measure_button = ttk.Button(
            map_header, text="📏", width=3, command=self._open_measurement,
        )
        measure_button.pack(side=tk.RIGHT)
        add_tooltip(measure_button, "Pomiar odległości")

        self.figure = Figure(figsize=(8, 6), dpi=100, facecolor=self.palette["map"])
        self.ax = self.figure.add_subplot(111)
        self.ax.set_position([0, 0, 1, 1])
        self.figure.subplots_adjust(left=0, right=1, bottom=0, top=1)
        self.ax.set_facecolor(self.palette["map"])
        self.ax.set_xlabel("")
        self.ax.set_ylabel("")
        self.ax.grid(True, linewidth=0.5, color=self.palette["grid"], alpha=0.65)
        self.ax.text(
            0.5, 0.5, "Dodaj dane z bazy PostGIS",
            transform=self.ax.transAxes, ha="center", va="center",
            fontsize=14, color=self.palette["muted"]
        )
        self.canvas = FigureCanvasTkAgg(self.figure, master=map_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        toolbar_row = ttk.Frame(map_frame)
        toolbar_row.pack(side=tk.BOTTOM, fill=tk.X)
        self.scale_var = tk.StringVar(master=self.root, value="1:—")
        scale_box = ttk.LabelFrame(toolbar_row, text="Skala", padding=(5, 2))
        scale_box.pack(side=tk.RIGHT, padx=(6, 0), pady=2)
        self._scale_label = self.scale_var.get()
        self._scale_denominator = None
        self._scale_magnification = 1.0
        self._scale_locked = tk.BooleanVar(master=self.root)
        self.scale_field = ttk.Entry(scale_box, textvariable=self.scale_var, width=15, justify=tk.CENTER)
        self.scale_field.pack(side=tk.LEFT)
        self.scale_field.bind("<Return>", self._apply_map_scale)
        self.scale_field.bind("<Escape>", self._cancel_map_scale_edit)
        self.scale_field.bind("<FocusOut>", lambda event: self.scale_var.set(self._scale_label))
        lock_button = ttk.Checkbutton(
            scale_box, text="🔒", variable=self._scale_locked, style="Toolbutton",
            command=self._toggle_scale_lock,
        )
        lock_button.pack(side=tk.LEFT, padx=(4, 0))
        add_tooltip(lock_button, "Zablokuj skalę. Kółko zmienia powiększenie widoku.")
        try:
            from pyproj import Geod
        except ImportError:
            self._scale_geod = None
        else:
            self._scale_geod = Geod(ellps="WGS84")
        self.canvas.mpl_connect("draw_event", self._update_map_scale)

        self.toolbar = PSIPToolbar(
            self.canvas,
            toolbar_row,
            self,
            pack_toolbar=False
        )
        self.toolbar.update()
        if hasattr(self.toolbar, "_message_label"):
            self.toolbar._message_label.pack_forget()
        self.toolbar.pack(side=tk.LEFT, anchor=tk.W)

        self._measure_window = None
        self._measure_points = []
        self._measure_segments = []
        self._measure_preview = None
        self._measure_active = False
        self._measure_lines = None
        self.canvas.mpl_connect("button_press_event", self._place_point)
        self.canvas.mpl_connect("button_press_event", self._on_measure_click)
        self.canvas.mpl_connect("motion_notify_event", self._on_measure_motion)
        self.canvas.mpl_connect("axes_leave_event", self._on_measure_leave)
        self.canvas.get_tk_widget().bind("<Escape>", self._cancel_point_capture, add="+")
        self.canvas.get_tk_widget().bind("<Escape>", self._close_measurement, add="+")

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

    def _update_map_scale(self, event=None, nominal_scale=None):
        left, right = self.ax.get_xlim()
        bottom, top = self.ax.get_ylim()
        latitude = (bottom + top) / 2
        renderer = event.renderer if event is not None else self.canvas.get_renderer()
        renderer_width = self.ax.get_window_extent(renderer).width
        widget = self.canvas.get_tk_widget()
        label = "1:—"
        self._scale_denominator = None

        if self._scale_geod is not None and renderer_width > 0 and abs(latitude) < 90:
            longitude = (left + right) / 2
            pixel_degrees = abs(right - left) / renderer_width
            sample_degrees = min(pixel_degrees, 1.0)
            if sample_degrees > 0:
                _, _, distance = self._scale_geod.inv(
                    longitude - sample_degrees / 2, latitude,
                    longitude + sample_degrees / 2, latitude,
                )
                pixels_per_inch = widget.winfo_fpixels("1i") * self.canvas.device_pixel_ratio
                denominator = distance * (pixel_degrees / sample_degrees) * pixels_per_inch / 0.0254
                if math.isfinite(denominator) and denominator > 0:
                    self._scale_denominator = denominator
                    if nominal_scale is not None:
                        self._scale_magnification = nominal_scale / denominator
                    displayed_scale = denominator * self._scale_magnification
                    label = f"1:{max(1, round(displayed_scale)):,}".replace(",", " ")

        self._scale_label = label
        if self.root.focus_get() is not self.scale_field and label != self.scale_var.get():
            self.scale_var.set(label)

    def _apply_map_scale(self, _event=None):
        raw = "".join(self.scale_var.get().split())
        if raw.startswith("1:"):
            raw = raw[2:]
        try:
            target = int(raw)
            if not 1 <= target <= 1_000_000_000_000:
                raise ValueError
        except ValueError:
            self._set_status("Wpisz skalę od 1:1 do 1:1 000 000 000 000, np. 1:25 000.")
            self.scale_field.selection_range(0, tk.END)
            return "break"

        if self._scale_denominator is None:
            self._set_status("Skala jest niedostępna dla obecnego widoku.")
            return "break"

        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        self._scale_magnification = 1.0
        factor = target / self._scale_denominator
        self._zoom_view(factor, (x0 + x1) / 2, (y0 + y1) / 2)
        self.canvas.get_tk_widget().focus_set()
        self._set_status(f"Ustawiono skalę 1:{target:,}.".replace(",", " "))
        return "break"

    def _cancel_map_scale_edit(self, _event=None):
        self.scale_var.set(self._scale_label)
        self.canvas.get_tk_widget().focus_set()
        return "break"

    def _open_measurement(self):
        self._cancel_point_capture()
        if self._measure_window is not None:
            self._measure_window.lift()
            return
        try:
            from pyproj import Geod
        except ImportError:
            messagebox.showerror("Pomiar odległości", "Zainstaluj pyproj, aby mierzyć odległości.", parent=self.root)
            return

        self._measure_geod = Geod(ellps="WGS84")
        if self.toolbar.mode:
            self.toolbar.pan()

        window = tk.Toplevel(self.root)
        self._measure_window = window
        window.title("Pomiar odległości")
        window.transient(self.root)
        window.resizable(False, False)
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        self._measure_status = ttk.Label(frame)
        self._measure_status.pack(anchor=tk.W, pady=(0, 6))
        self._measure_table = ttk.Treeview(
            frame, columns=("length",), show="headings", height=5, selectmode="none"
        )
        self._measure_table.heading("length", text="Odcinki [m]")
        self._measure_table.column("length", width=260, anchor=tk.E)
        self._measure_table.pack(fill=tk.X)
        self._measure_total = ttk.Label(frame)
        self._measure_total.pack(anchor=tk.E, pady=6)
        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Nowy pomiar", command=self._reset_measurement).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Zamknij", command=self._close_measurement).pack(side=tk.RIGHT)
        window.protocol("WM_DELETE_WINDOW", self._close_measurement)
        window.bind("<Escape>", self._close_measurement)
        self._reset_measurement()

    def _close_measurement(self, _event=None):
        if self._measure_window is None:
            return
        self._measure_window.destroy()
        self._measure_window = None
        self._reset_measurement()

    def _reset_measurement(self, _event=None):
        self._measure_points.clear()
        self._measure_segments.clear()
        self._measure_preview = None
        self._measure_active = self._measure_window is not None
        if self._measure_active:
            self._measure_table.delete(*self._measure_table.get_children())
            self._measure_status.configure(text="Lewy klik dodaje punkt. Prawy kończy pomiar.")
        self._refresh_measurement()

    def _measurement_point(self, event):
        if event.inaxes is self.ax and event.xdata is not None and event.ydata is not None:
            if math.isfinite(event.xdata) and -90 <= event.ydata <= 90:
                return (event.xdata, event.ydata)
        return None

    def _segment_length(self, start, end):
        return self._measure_geod.inv(*start, *end)[2]

    def _on_measure_click(self, event):
        if not self._measure_active or self.toolbar.mode or event.button not in (1, 3):
            return
        point = self._measurement_point(event)
        if point is None:
            return
        self.canvas.get_tk_widget().focus_set()
        if event.button == 1:
            if self._measure_points:
                length = self._segment_length(self._measure_points[-1], point)
                self._measure_segments.append(length)
                self._measure_table.insert("", tk.END, values=(self._format_measurement(length),))
                self._measure_table.see(self._measure_table.get_children()[-1])
            self._measure_points.append(point)
        elif self._measure_points:
            self._measure_active = False
            self._measure_status.configure(text="Pomiar zakończony. Esc czyści wynik.")
        self._measure_preview = None
        self._refresh_measurement()

    def _on_measure_motion(self, event):
        if self._measure_active and self._measure_points:
            point = None if self.toolbar.mode else self._measurement_point(event)
            if point != self._measure_preview:
                self._measure_preview = point
                self._refresh_measurement()

    def _on_measure_leave(self, _event):
        if self._measure_preview is not None:
            self._measure_preview = None
            self._refresh_measurement()

    @staticmethod
    def _format_measurement(value):
        return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " m"

    def _refresh_measurement(self):
        self._draw_measurement()
        if self._measure_window is not None:
            total = math.fsum(self._measure_segments)
            label = f"Suma: {self._format_measurement(total)}"
            if self._measure_preview is not None:
                preview = self._segment_length(self._measure_points[-1], self._measure_preview)
                label += f"  |  Podgląd: {self._format_measurement(preview)}"
            self._measure_total.configure(text=label)
        self.canvas.draw_idle()

    def _draw_measurement(self):
        if self._measure_lines is None:
            if not self._measure_points:
                return
            line, = self.ax.plot([], [], color="#ffd34d", marker="o",
                                 linewidth=2, markersize=5, zorder=float("inf"), scalex=False, scaley=False)
            preview, = self.ax.plot([], [], color="#ffd34d", linestyle="--",
                                    linewidth=1.5, zorder=float("inf"), scalex=False, scaley=False)
            self._measure_lines = (line, preview)
        line, preview = self._measure_lines
        line.set_data(*(zip(*self._measure_points) if self._measure_points else ([], [])))
        if self._measure_preview is None:
            preview.set_data([], [])
        else:
            preview.set_data(*zip(self._measure_points[-1], self._measure_preview))

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

    def _toggle_scale_lock(self):
        self._scale_magnification = 1.0
        self.canvas.draw_idle()

    def _on_map_scroll(self, event):
        if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
            return
        factor = 1 / 1.2 if event.button == "up" else 1.2
        locked = self._scale_locked.get() and self._scale_denominator is not None
        if locked:
            nominal_scale = self._scale_denominator * self._scale_magnification
        self._zoom_view(factor, event.xdata, event.ydata)
        if locked:
            # Scrolling changes both the extent and its center. Recompute the
            # display magnification using the ground distance at the new center.
            self._update_map_scale(nominal_scale=nominal_scale)

    def _zoom_view(self, factor, x, y):
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
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
            ("Użytkownik", "user"),
            ("Hasło", "password"),
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

    def _add_layer_sources(self, menu):
        menu.add_command(label="Z dysku…", command=self.add_local_layer_dialog)
        menu.add_command(label="Z bazy PostGIS…", command=self.add_data_dialog)
        menu.add_command(label="Nowa warstwa punktowa…", command=self._create_point_layer)

    def _create_point_layer(self):
        window = tk.Toplevel(self.root)
        window.title("Nowa warstwa punktowa")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)

        name = tk.StringVar(master=window)
        mode = tk.StringVar(master=window, value="temporary")
        filename = tk.StringVar(master=window)
        error = tk.StringVar(master=window)
        ttk.Label(frame, text="Nazwa warstwy").grid(row=0, column=0, sticky="w", padx=(0, 8))
        name_field = ttk.Entry(frame, textvariable=name)
        name_field.grid(row=0, column=1, columnspan=2, sticky="ew")
        ttk.Radiobutton(
            frame, text="Warstwa tymczasowa", variable=mode, value="temporary",
        ).grid(row=1, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Radiobutton(
            frame, text="Zapisz warstwę", variable=mode, value="file",
        ).grid(row=2, columnspan=3, sticky="w")
        ttk.Label(frame, text="Plik").grid(row=3, column=0, sticky="w")
        path_field = ttk.Entry(frame, textvariable=filename, width=36)
        path_field.grid(row=3, column=1, sticky="ew")

        def browse():
            selected_type = tk.StringVar(master=window)
            path = filedialog.asksaveasfilename(
                parent=window, title="Zapisz warstwę punktową",
                initialfile=name.get().strip(), typevariable=selected_type,
                filetypes=[("GeoJSON", "*.geojson"), ("Shapefile", "*.shp")],
                confirmoverwrite=False,
            )
            if path:
                target = Path(path)
                if not target.suffix:
                    extension = ".shp" if selected_type.get().startswith("Shapefile") else ".geojson"
                    target = target.with_suffix(extension)
                filename.set(str(target))

        browse_button = ttk.Button(frame, text="Przeglądaj…", command=browse)
        browse_button.grid(row=3, column=2, padx=(6, 0))
        ttk.Label(frame, textvariable=error, style="Error.TLabel", wraplength=390).grid(
            row=4, columnspan=3, sticky="w", pady=(6, 0)
        )

        def update_mode(*_args):
            state = "normal" if mode.get() == "file" else "disabled"
            path_field.configure(state=state)
            browse_button.configure(state=state)
            error.set("")

        def create(_event=None):
            title = name.get().strip()
            if not title:
                error.set("Podaj nazwę warstwy.")
                return
            if any(layer.editable_points and layer.info.table == title for layer in self.layers):
                error.set("Ta nazwa jest już używana.")
                return

            path = None
            if mode.get() == "file":
                value = filename.get().strip()
                if not value:
                    error.set("Podaj ścieżkę do pliku.")
                    return
                path = Path(value).expanduser()
                if not path.suffix:
                    path = path.with_suffix(".geojson")
                extension = path.suffix.lower()
                if extension not in {".geojson", ".shp"}:
                    error.set("Wybierz plik .geojson lub .shp.")
                    return
                targets = self._shapefile_targets(path) if extension == ".shp" else [path]
                if any(target.exists() for target in targets):
                    error.set("Ten plik już istnieje. Wybierz inną nazwę.")
                    return

            try:
                self._add_point_layer(title, path)
            except (ImportError, OSError, ValueError) as exc:
                error.set(f"Nie udało się zapisać warstwy: {exc}")
                return
            window.destroy()
            self.canvas.get_tk_widget().focus_set()

        actions = ttk.Frame(frame)
        actions.grid(row=5, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(actions, text="Anuluj", command=window.destroy).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(actions, text="Utwórz", command=create).pack(side=tk.LEFT)
        mode.trace_add("write", update_mode)
        update_mode()
        window.bind("<Escape>", lambda event: window.destroy())
        window.bind("<Return>", create)
        window.grab_set()
        name_field.focus_set()

    def _save_point_layer(self, layer, path):
        if path.suffix.lower() == ".shp":
            from pyproj import CRS
            return self._save_shapefile(layer, path, CRS.from_epsg(4326))
        return self._save_geojson(layer, path)

    def _add_point_layer(self, name, path=None):
        info = LayerInfo("tymczasowa", name, "geom", 4326, "POINT", False, False)
        layer = MapLayer(
            info, [], color="#ffb454", editable_points=True,
            attribute_types={"nazwa": "text"}, attribute_rows=[],
        )
        if path is not None:
            path = path.resolve()
            if any(current.source_path == path for current in self.layers):
                raise ValueError("Ten plik jest już otwarty jako warstwa.")
            self._save_point_layer(layer, path)
            layer.source_path = path

        self.layers.insert(0, layer)
        self._refresh_layer_list()
        key = self._layer_tree_key(layer)
        self.layer_list.selection_set(key)
        self.layer_list.focus(key)
        self.layer_list.see(key)
        self.draw_map()
        self._start_point_capture(layer)

    def _start_point_capture(self, layer):
        if self._measure_window is not None:
            self._close_measurement()
        if self.toolbar.mode:
            self.toolbar.pan()
        self._point_target = layer
        widget = self.canvas.get_tk_widget()
        widget.configure(cursor="crosshair")
        widget.focus_set()
        self._set_status("Kliknij miejsce na mapie, aby dodać punkt. Esc anuluje.")

    def _cancel_point_capture(self, _event=None):
        if self._point_target is not None:
            self._point_target = None
            self.canvas.get_tk_widget().configure(cursor="")
            self._set_status("Anulowano dodawanie punktu.")

    def _place_point(self, event):
        layer = self._point_target
        if layer is None or event.button != 1 or self.toolbar.mode:
            return
        coordinates = self._measurement_point(event)
        if coordinates is None:
            return
        if not any(current is layer for current in self.layers):
            self._cancel_point_capture()
            return
        name = simpledialog.askstring(
            "Dodaj punkt", "Nazwa punktu (opcjonalnie):", parent=self.root,
        )
        self.canvas.get_tk_widget().focus_set()
        if name is None:
            return
        layer.geometries.append(Point(*coordinates))
        layer.attribute_rows.append({"nazwa": name.strip() or None})
        if layer.source_path is not None:
            try:
                self._save_point_layer(layer, layer.source_path)
            except (ImportError, OSError, ValueError) as error:
                layer.geometries.pop()
                layer.attribute_rows.pop()
                messagebox.showerror("Nie udało się zapisać punktu", str(error), parent=self.root)
                return
        layer.visible = True
        self._refresh_layer_list()
        self.draw_map()
        self._set_status(f"Dodano punkt do warstwy {layer.info.table}. Kliknij dalej lub naciśnij Esc.")

    def add_local_layer_dialog(self):
        filename = filedialog.askopenfilename(
            parent=self.root, title="Dodaj warstwę z dysku",
            filetypes=[
                ("Warstwy wektorowe", ("*.shp", "*.geojson")),
                ("ESRI Shapefile", "*.shp"),
                ("GeoJSON", "*.geojson"),
            ],
        )
        if not filename:
            return

        path = Path(filename).resolve()
        try:
            if path.suffix.lower() == ".geojson":
                layer = self._read_geojson(path)
            else:
                from pyproj import CRS
                projection = path.with_suffix(".prj")
                crs = None
                if projection.exists():
                    try:
                        crs = CRS.from_wkt(projection.read_text(encoding="utf-8-sig"))
                    except (ValueError, RuntimeError):
                        pass
                if crs is None:
                    epsg = simpledialog.askinteger(
                        "Układ współrzędnych",
                        "Brak poprawnego pliku PRJ. Podaj kod EPSG danych, np. 2180 lub 4326.",
                        minvalue=1, parent=self.root,
                    )
                    if epsg is None:
                        return
                    crs = CRS.from_epsg(epsg)
                layer = self._read_shapefile(path, crs)
        except ImportError:
            messagebox.showerror(
                "Dodaj warstwę z dysku",
                "Do otwierania SHP zainstaluj biblioteki: py -3.12 -m pip install pyshp pyproj",
                parent=self.root,
            )
            return
        except Exception as error:
            messagebox.showerror("Nie udało się otworzyć warstwy", str(error), parent=self.root)
            return

        key = self._layer_tree_key(layer)
        self.layers = [current for current in self.layers if self._layer_tree_key(current) != key]
        self.layers.insert(0, layer)
        self._refresh_layer_list()
        self.layer_list.selection_set(key)
        self.layer_list.focus(key)
        self.layer_list.see(key)
        self.draw_map()
        self.zoom_to_selected_layer()
        self._set_status(f"Wczytano z dysku: {layer.display_name}.")

    def _read_geojson(self, path):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("Plik nie zawiera obiektu GeoJSON.")
        if data.get("type") == "FeatureCollection":
            features = data["features"]
        elif data.get("type") == "Feature":
            features = [data]
        else:
            features = [{"geometry": data, "properties": {}}]

        geometries = []
        rows = []
        field_types = {}
        for feature in features:
            if feature.get("geometry") is None:
                continue
            geometry = shape(feature["geometry"])
            if geometry.is_empty:
                continue
            if not all(math.isfinite(value) for value in geometry.bounds):
                raise ValueError("GeoJSON zawiera nieprawidłowe współrzędne.")
            properties = feature.get("properties") or {}
            if not isinstance(properties, dict):
                raise ValueError("Atrybuty GeoJSON muszą być zapisane jako obiekt.")
            geometries.append(geometry)
            rows.append(properties)
            for field, value in properties.items():
                kind = field_types.setdefault(field, "numeric")
                if value is not None and type(value) not in (int, float):
                    field_types[field] = "text"

        if not geometries and features:
            raise ValueError("Plik nie zawiera geometrii do wyświetlenia.")
        kinds = {geometry.geom_type.upper() for geometry in geometries}
        kind = kinds.pop() if len(kinds) == 1 else "GEOMETRYCOLLECTION"
        info = LayerInfo("plik", path.name, "GeoJSON", 4326, kind, False, False)
        return MapLayer(
            info, geometries, source_path=path, attribute_types=field_types, attribute_rows=rows,
        )

    def export_geojson_dialog(self, layer):
        filename = filedialog.asksaveasfilename(
            parent=self.root, title="Zapisz warstwę jako GeoJSON",
            initialfile=layer.source_path.stem if layer.source_path else layer.info.table,
            defaultextension=".geojson", filetypes=[("GeoJSON", "*.geojson")],
        )
        if not filename:
            return

        path = Path(filename).resolve()
        try:
            count = self._save_geojson(layer, path)
        except Exception as error:
            messagebox.showerror("Nie udało się zapisać GeoJSON", str(error), parent=self.root)
            return

        self._set_status(f"Zapisano {count} obiektów do {path.name}.")
        messagebox.showinfo("Eksport GeoJSON", f"Zapisano {count} obiektów w pliku {path.name}.", parent=self.root)

    def _save_geojson(self, layer, path):
        with tempfile.TemporaryDirectory(dir=path.parent) as directory:
            staged = Path(directory) / path.name
            count = self._write_geojson(layer, staged)
            staged.replace(path)
        return count

    def _write_geojson(self, layer, path):
        _, geometries, rows = self._layer_export_data(layer)

        def encode(value):
            if isinstance(value, Decimal) and value.is_finite():
                return int(value) if value == value.to_integral_value() else float(value)
            return str(value)

        with path.open("w", encoding="utf-8") as stream:
            stream.write('{"type":"FeatureCollection","features":[\n')
            for index, (geometry, row) in enumerate(zip(geometries, rows)):
                if index:
                    stream.write(",\n")
                json.dump(
                    {"type": "Feature", "geometry": geometry.__geo_interface__, "properties": row},
                    stream, ensure_ascii=False, default=encode, allow_nan=False,
                )
            stream.write("\n]}\n")
        return len(geometries)

    def _read_shapefile(self, path, crs):
        import shapefile
        from pyproj import Transformer

        projection = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        cpg = path.with_suffix(".cpg")
        encoding = cpg.read_text(encoding="utf-8-sig").strip() if cpg.exists() else "utf-8"
        encoding = encoding or "utf-8"
        if encoding == "65001":
            encoding = "utf-8"
        elif encoding.isdecimal():
            encoding = f"cp{encoding}"

        geometries = []
        rows = []
        with shapefile.Reader(str(path), encoding=encoding) as reader:
            field_types = {
                field.name: "numeric" if field.field_type in ("N", "F") else "text"
                for field in reader.fields[1:]
            }
            empty_point_file = len(reader) == 0 and reader.shapeType == shapefile.POINT
            for item in reader.iterShapeRecords():
                if item.shape.shapeType == shapefile.NULL:
                    continue
                geometry = transform(projection.transform, shape(item.shape.__geo_interface__))
                if not geometry.is_empty:
                    geometries.append(geometry)
                    rows.append(item.record.as_dict())

        if (not geometries and not empty_point_file) or not all(math.isfinite(v) for g in geometries for v in g.bounds):
            raise ValueError("Plik nie zawiera geometrii, którą można wyświetlić w podanym układzie.")
        kind = geometries[0].geom_type.upper() if geometries else "POINT"
        info = LayerInfo("plik", path.name, "SHP", crs.to_epsg(), kind, False, False)
        return MapLayer(
            info, geometries, source_path=path, attribute_types=field_types, attribute_rows=rows,
        )

    def export_shapefile_dialog(self, layer):
        filename = filedialog.asksaveasfilename(
            parent=self.root, title="Zapisz warstwę jako SHP",
            initialfile=layer.source_path.stem if layer.source_path else layer.info.table,
            defaultextension=".shp", filetypes=[("ESRI Shapefile", "*.shp")],
            confirmoverwrite=False,
        )
        if not filename:
            return

        path = Path(filename).resolve()
        epsg = simpledialog.askinteger(
            "Układ współrzędnych eksportu", "Podaj docelowy kod EPSG:",
            initialvalue=layer.info.srid if layer.info.srid and layer.info.srid > 0 else 4326,
            minvalue=1, parent=self.root,
        )
        if epsg is None:
            return

        targets = self._shapefile_targets(path)
        if any(target.exists() for target in targets) and not messagebox.askyesno(
            "Nadpisać warstwę?", f"Zastąpić istniejące pliki warstwy {path.stem}?",
            parent=self.root,
        ):
            return

        try:
            from pyproj import CRS

            count = self._save_shapefile(layer, path, CRS.from_epsg(epsg))
        except ImportError:
            messagebox.showerror(
                "Eksport SHP", "Zainstaluj biblioteki: py -3.12 -m pip install pyshp pyproj",
                parent=self.root,
            )
            return
        except Exception as error:
            messagebox.showerror("Nie udało się zapisać SHP", str(error), parent=self.root)
            return

        self._set_status(f"Zapisano {count} obiektów do {path.with_suffix('.shp').name}.")
        messagebox.showinfo(
            "Eksport SHP", f"Zapisano {count} obiektów w pliku:\n{path.with_suffix('.shp')}",
            parent=self.root,
        )

    @staticmethod
    def _shapefile_targets(path):
        return [path.with_suffix(extension) for extension in (".shp", ".shx", ".dbf", ".prj", ".cpg")]

    def _save_shapefile(self, layer, path, crs):
        targets = self._shapefile_targets(path)
        with tempfile.TemporaryDirectory(dir=path.parent) as directory:
            staging = Path(directory)
            staged = staging / path.with_suffix(".shp").name
            count = self._write_shapefile(layer, staged, crs)
            backup = staging / "backup"
            backup.mkdir()
            saved = []
            installed = []
            try:
                for target in targets:
                    if target.exists():
                        target.replace(backup / target.name)
                        saved.append(target)
                for target in targets:
                    (staging / target.name).replace(target)
                    installed.append(target)
            except OSError:
                for target in installed:
                    target.unlink()
                for target in saved:
                    (backup / target.name).replace(target)
                raise
        return count

    def _layer_export_data(self, layer):
        if layer.attribute_rows is not None:
            return layer.attribute_types, layer.geometries, layer.attribute_rows

        info = layer.info
        fields = self._searchable_attributes(info)
        geometry = sql.Identifier(info.geom_column)
        columns = [sql.SQL("ST_AsBinary(ST_Transform({}, 4326))").format(geometry)]
        columns.extend(sql.Identifier(field) for field in fields)
        query = sql.SQL("SELECT {} FROM {}.{} WHERE {} IS NOT NULL AND NOT ST_IsEmpty({})").format(
            sql.SQL(", ").join(columns), sql.Identifier(info.schema), sql.Identifier(info.table),
            geometry, geometry,
        )
        params = []
        if layer.resolution is not None and info.has_resolution:
            query += sql.SQL(" AND resolution = %s")
            params.append(layer.resolution)

        geometries = []
        rows = []
        with closing(self._connect()) as conn, conn.cursor() as cursor:
            cursor.execute(query, params)
            for raw_geometry, *values in cursor:
                geometries.append(wkb.loads(bytes(raw_geometry)))
                rows.append(dict(zip(fields, values)))
        return fields, geometries, rows

    def _write_shapefile(self, layer, path, crs):
        import shapefile
        from pyproj import Transformer

        fields, geometries, rows = self._layer_export_data(layer)
        kinds = {geometry.geom_type for geometry in geometries}
        if not kinds and not layer.editable_points:
            raise ValueError("Warstwa nie zawiera obiektów do zapisania.")
        if not kinds or kinds == {"Point"}:
            shape_type = shapefile.POINT
        elif kinds <= {"Point", "MultiPoint"}:
            shape_type = shapefile.MULTIPOINT
        elif kinds <= {"LineString", "MultiLineString"}:
            shape_type = shapefile.POLYLINE
        elif kinds <= {"Polygon", "MultiPolygon"}:
            shape_type = shapefile.POLYGON
        else:
            raise ValueError("SHP wymaga jednego rodzaju geometrii w warstwie.")

        project = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        integer_types = {"int2", "int4", "int8"}
        number_types = {"numeric", "float4", "float8"}
        with shapefile.Writer(str(path), shapeType=shape_type, encoding="utf-8") as writer:
            names = set()
            for field, kind in fields.items():
                base = re.sub(r"[^A-Za-z0-9_]", "_", field) or "pole"
                name = base[:10]
                suffix = 1
                while name.upper() in names:
                    tail = str(suffix)
                    name = base[:10 - len(tail)] + tail
                    suffix += 1
                names.add(name.upper())
                if kind in integer_types:
                    writer.field(name, "N", size=20)
                elif kind in number_types:
                    writer.field(name, "F", size=24, decimal=12)
                else:
                    writer.field(name, "C", size=254)

            if not fields:
                writer.field("ID", "N", size=12)

            for index, (geometry, row) in enumerate(zip(geometries, rows), start=1):
                geometry = force_2d(transform(project.transform, geometry))
                if not all(math.isfinite(value) for value in geometry.bounds):
                    raise ValueError("Nie można przeliczyć geometrii na wybrany układ.")
                if shape_type == shapefile.MULTIPOINT and isinstance(geometry, Point):
                    geometry = MultiPoint([geometry])
                elif isinstance(geometry, Polygon):
                    geometry = orient(geometry, sign=-1)
                elif isinstance(geometry, MultiPolygon):
                    geometry = MultiPolygon([orient(part, sign=-1) for part in geometry.geoms])

                values = []
                for field, kind in fields.items():
                    value = row.get(field)
                    if value is not None:
                        if kind in number_types:
                            if len(f"{value:.12f}") > 24:
                                raise ValueError(f"Wartość w polu {field} nie mieści się w formacie DBF.")
                        elif kind not in integer_types:
                            value = str(value)
                            if len(value.encode("utf-8")) > 254:
                                raise ValueError(f"Tekst w polu {field} przekracza limit 254 bajtów DBF.")
                    values.append(value)
                if isinstance(geometry, Point):
                    writer.point(geometry.x, geometry.y)
                else:
                    writer.shape(geometry.__geo_interface__)
                writer.record(*(values if fields else [index]))

        path.with_suffix(".prj").write_text(crs.to_wkt(version="WKT1_ESRI"), encoding="utf-8")
        path.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")
        return len(geometries)

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
                    "Wybierz warstw?",
                    "Zaznacz wiersz, kt?ry chcesz doda?.",
                    parent=win
                )
                return

            info, resolution = row_map[selected[0]]

            try:
                if info.has_iop:
                    import json
                    import subprocess
                    import sys
                    from pathlib import Path

                    root_dir = Path(__file__).resolve().parents[1]
                    config_path = root_dir / "config" / "iop_config.json"
                    script_path = root_dir / "scripts" / "run_iop.py"
                    sql_path = root_dir / "scripts" / "create_iop_color_layers.sql"

                    defaults = {
                        "k_land": 1.6,
                        "k_hydro": 1.2,
                        "k_roads": 0.8,
                        "k_buildings": 0.4
                    }

                    old_config = config_path.read_text(encoding="utf-8")

                    try:
                        config = json.loads(old_config)
                        config["weights"] = defaults

                        temp_path = config_path.with_suffix(".json.tmp")
                        temp_path.write_text(
                            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8"
                        )
                        temp_path.replace(config_path)

                        self._set_status(
                            "IOP: przeliczanie na wagach domy?lnych..."
                        )
                        win.update_idletasks()

                        result = subprocess.run(
                            [sys.executable, str(script_path), "--replace"],
                            cwd=str(root_dir),
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            timeout=300
                        )

                        if result.returncode != 0:
                            raise RuntimeError(
                                "B??d przeliczania IOP:\n"
                                + result.stdout[-3000:]
                                + "\n"
                                + result.stderr[-5000:]
                            )

                        self._set_status(
                            "IOP: aktualizacja kolor?w..."
                        )
                        win.update_idletasks()

                        with self._connect() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    sql_path.read_text(encoding="utf-8")
                                )

                    finally:
                        config_path.write_text(
                            old_config,
                            encoding="utf-8"
                        )

                layer = self._load_layer(info, resolution, fit=True, move_to_top=True)

                if info.has_iop:
                    layer._iop_weights = [
                        1.6, 1.2, 0.8, 0.4
                    ]

                win.destroy()

            except Exception as exc:
                messagebox.showerror(
                    "Nie uda?o si? wczyta? warstwy",
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
    def _iop_color(value, mode):
        if value is None or math.isnan(value):
            return IOP_MISSING_COLOR

        if mode == "contrast":
            magnitude = min(1.0, abs(value) / 0.5) ** 0.65
            value = math.copysign(magnitude, value)
        else:
            magnitude = min(1.0, abs(value))

        endpoint = IOP_RED if value < 0 else IOP_GREEN
        channels = (
            round(white + magnitude * (target - white))
            for white, target in zip(IOP_WHITE, endpoint)
        )
        return "#{:02x}{:02x}{:02x}".format(*channels)

    def _update_iop_color_scale(self):
        mode = self.iop_color_scale.get()
        for layer in self.layers:
            if layer.iop_values is not None:
                layer.feature_colors = [
                    self._iop_color(value, mode) for value in layer.iop_values
                ]
        self._refresh_layer_list()
        self.draw_map(fit=False)

    def _load_layer(self, info, resolution=None, fit=True, show_errors=True, move_to_top=False):
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
            mode = self.iop_color_scale.get()
            feature_colors = [self._iop_color(value, mode) for value in feature_values]

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

        previous_index = next(
            (index for index, layer in enumerate(self.layers)
             if layer.info.full_name == info.full_name and layer.resolution == resolution),
            None,
        )
        if previous_index is not None:
            self.layers.pop(previous_index)

        layer = MapLayer(
            info=info,
            geometries=geometries,
            resolution=resolution,
            visible=True,
            color=color,
            feature_colors=feature_colors,
            iop_min=iop_min,
            iop_max=iop_max,
            iop_values=feature_values if info.has_iop else None,
        )
        position = 0 if previous_index is None or move_to_top else previous_index
        self.layers.insert(position, layer)

        self._refresh_layer_list()
        self.draw_map(fit=fit)

        status_text = (
            f"Wczytano {info.full_name}: {len(geometries)} obiektów."
            + (f" (limit {MAX_FEATURES})" if len(geometries) == MAX_FEATURES else "")
        )
        if info.has_iop and iop_min is not None and iop_max is not None:
            status_text += f" Zakres IOP: {iop_min:.4f} do {iop_max:.4f}."
        self._set_status(status_text)
        return layer

    def _layer_tree_key(self, layer):
        if layer.source_path is not None:
            return f"file:{layer.source_path}"
        return f"layer:{layer.info.schema!r}:{layer.info.table!r}:{layer.resolution}"

    def _make_legend_swatch(self, color):
        image = tk.PhotoImage(
            master=self.root, width=12, height=12
        )
        image.put("#777777", to=(0, 0, 12, 12))
        image.put(color, to=(1, 1, 11, 11))
        return image

    def _iop_legend_entries(self, layer):
        if not layer.info.has_iop:
            geom_type = layer.info.geom_type.upper()
            labels = {
                "POLYGON": "Poligony",
                "MULTIPOLYGON": "Poligony",
                "LINESTRING": "Linie",
                "MULTILINESTRING": "Linie",
                "POINT": "Punkty",
                "MULTIPOINT": "Punkty",
            }
            return [(layer.color, labels.get(geom_type, "Styl warstwy"))]

        return self._iop_scale_entries()

    def _iop_scale_entries(self):
        mode = self.iop_color_scale.get()
        ticks = (
            (-0.50, -0.17, 0.0, 0.17, 0.50) if mode == "contrast"
            else (-1.0, -0.50, 0.0, 0.50, 1.0)
        )
        entries = []
        for value in ticks:
            label = f"{value:+.2f}" if value else "0.00"
            label = label.replace("-", "−").replace(".", ",")
            entries.append((self._iop_color(value, mode), label))
        entries.append((self._iop_color(None, mode), "Brak wyniku IOP"))
        return entries

    def _refresh_layer_list(self):
        selected = self.layer_list.selection()
        selected_key = selected[0] if selected else None
        if selected_key and self.layer_list.parent(selected_key):
            selected_key = self.layer_list.parent(selected_key)
        open_states = {
            iid: self.layer_list.item(iid, "open")
            for iid in self.layer_list.get_children("")
        }

        children = self.layer_list.get_children("")
        if children:
            self.layer_list.delete(*children)

        self._legend_images = []

        for layer in self.layers:
            iid = self._layer_tree_key(layer)
            open_state = open_states.get(iid, True)

            marker = "●" if layer.visible else "○"

            self.layer_list.insert(
                "", tk.END,
                iid=iid,
                text=f"{marker}  {layer.display_name}",
                open=open_state
            )

            for color, label in self._iop_legend_entries(layer):
                image = self._make_legend_swatch(color)
                self._legend_images.append(image)

                self.layer_list.insert(
                    iid, tk.END,
                    text=label,
                    image=image,
                    tags=("legend",)
                )

        if selected_key and self.layer_list.exists(selected_key):
            self.layer_list.selection_set(selected_key)
            self.layer_list.focus(selected_key)

    def _layer_index_for_tree_item(self, iid):
        if not iid:
            return None
        key = self.layer_list.parent(iid) or iid
        return next(
            (index for index, layer in enumerate(self.layers)
             if self._layer_tree_key(layer) == key),
            None,
        )

    def move_selected_layer(self, offset):
        source = self._selected_layer_index()
        if source is not None:
            self._move_layer(source, source + offset)

    def _move_layer(self, source, destination):
        if source == destination or not 0 <= destination < len(self.layers):
            return
        layer = self.layers.pop(source)
        self.layers.insert(destination, layer)
        self._refresh_layer_list()
        self.layer_list.see(self._layer_tree_key(layer))
        self.draw_map(fit=False)

    def _start_layer_drag(self, event):
        iid = self.layer_list.identify_row(event.y)
        self._layer_drag = None
        if (
            iid and not self.layer_list.parent(iid)
            and not self.layer_list.identify_element(event.x, event.y).endswith("indicator")
        ):
            self._layer_drag = (iid, event.y)

    def _drag_layer(self, event):
        if self._layer_drag is None or abs(event.y - self._layer_drag[1]) < 5:
            return
        self.layer_list.configure(cursor="fleur")
        if event.y < 15:
            self.layer_list.yview_scroll(-1, "units")
        elif event.y > self.layer_list.winfo_height() - 15:
            self.layer_list.yview_scroll(1, "units")
        return "break"

    def _drop_layer(self, event):
        drag, self._layer_drag = self._layer_drag, None
        self.layer_list.configure(cursor="")
        if drag is None or abs(event.y - drag[1]) < 5:
            return
        source = self._layer_index_for_tree_item(drag[0])
        target = self._layer_index_for_tree_item(self.layer_list.identify_row(event.y))
        if source is not None:
            if target is None:
                target = 0 if event.y <= 0 else len(self.layers) - 1
            self.layer_list.selection_set(drag[0])
            self._move_layer(source, target)
        return "break"

    def _selected_layer_index(self):
        selection = self.layer_list.selection()
        if not selection:
            return None
        return self._layer_index_for_tree_item(selection[0])

    def _on_layer_tree_double_click(self, event):
        iid = self.layer_list.identify_row(event.y)
        if not iid:
            return "break"

        index = self._layer_index_for_tree_item(iid)
        if index is None or self.layer_list.parent(iid):
            return "break"

        self.layer_list.selection_set(iid)
        self.toggle_selected_layer()
        return "break"

    def show_layer_context_menu(self, event):
        iid = self.layer_list.identify_row(event.y)
        if not iid:
            return "break"

        index = self._layer_index_for_tree_item(iid)
        if index is None:
            return "break"

        self.layer_list.selection_set(iid)
        self.layer_list.focus(iid)

        layer = self.layers[index]
        menu = tk.Menu(self.root, tearoff=0)

        if layer.editable_points:
            menu.add_command(label="Dodaj punkt", command=lambda: self._start_point_capture(layer))
            menu.add_separator()

        if layer.info.has_iop:
            menu.add_command(
                label="Wskaźnik IOP",
                command=lambda: self.show_iop_dialog(index)
            )
            menu.add_command(label="Edytuj styl", command=self.show_iop_style_dialog)
            menu.add_separator()

        menu.add_command(
            label="Przybliż do warstwy",
            command=self.zoom_to_selected_layer
        )
        menu.add_command(
            label="Właściwości",
            command=self.show_selected_layer_properties
        )

        export_menu = tk.Menu(menu, tearoff=False)
        export_menu.add_command(
            label="Zapisz jako SHP…", command=lambda: self.export_shapefile_dialog(layer)
        )
        export_menu.add_command(
            label="Zapisz jako GeoJSON…", command=lambda: self.export_geojson_dialog(layer)
        )
        menu.add_cascade(label="Eksportuj", menu=export_menu)

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

        return "break"

    def show_iop_style_dialog(self):
        window = tk.Toplevel(self.root)
        window.title("Edytuj styl IOP")
        window.transient(self.root)
        window.resizable(False, False)

        frame = ttk.Frame(window, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="Skala barw IOP").pack(anchor=tk.W, pady=(0, 8))
        selection = tk.StringVar(master=window, value=self.iop_color_scale.get())

        def apply():
            self.iop_color_scale.set(selection.get())
            self._update_iop_color_scale()

        for mode, label in (
            ("reference", "Referencyjna −1…+1"),
            ("contrast", "Kontrastowa −0,5…+0,5"),
        ):
            ttk.Radiobutton(
                frame, text=label, value=mode, variable=selection,
                command=apply,
            ).pack(anchor=tk.W)

        ttk.Button(frame, text="Zamknij", command=window.destroy).pack(
            anchor=tk.E, pady=(12, 0)
        )
        window.bind("<Escape>", lambda event: window.destroy())
        window.wait_visibility()
        window.grab_set()

    def show_iop_dialog(self, layer_index):
        layer = self.layers[layer_index]

        window = tk.Toplevel(self.root)
        window.title("Wskaźnik IOP")
        window.geometry("410x440")
        window.resizable(False, False)
        window.configure(bg=self.palette["bg"])
        window.transient(self.root)

        bg = self.palette["bg"]
        panel = self.palette["panel"]
        fg = self.palette["text"]
        muted = self.palette["muted"]

        frame = tk.Frame(window, bg=bg, padx=22, pady=18)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            frame,
            text="KONFIGURACJA WSKAŹNIKA IOP",
            bg=bg, fg=fg,
            font=("Segoe UI", 13, "bold")
        ).pack(anchor="w", pady=(0, 5))

        tk.Label(
            frame,
            text=layer.display_name,
            bg=bg, fg=muted,
            font=("Segoe UI", 9),
            wraplength=360
        ).pack(anchor="w", pady=(0, 18))

        defaults = [1.6, 1.2, 0.8, 0.4]

        # Wagi przypisane do konkretnej instancji warstwy.
        # Nowo dodana warstwa zawsze zaczyna od warto?ci domy?lnych.
        initial_values = list(
            getattr(layer, "_iop_weights", defaults)
        )

        if (
            len(initial_values) != 4
            or any(not 0.0 <= float(v) <= 2.0 for v in initial_values)
            or sum(initial_values) <= 0
        ):
            initial_values = defaults.copy()

        variables = []
        value_labels = []

        sum_label = tk.Label(
            frame,
            text="Suma wag: 4.0",
            bg=panel, fg=fg,
            font=("Segoe UI", 11, "bold"),
            padx=10, pady=10
        )
        sum_label.pack(fill=tk.X, pady=(12, 15))

        def update_sum(*args):
            total = sum(v.get() for v in variables)
            sum_label.config(text=f"Suma wag: {total:.1f}")

            for i, variable in enumerate(variables):
                value_labels[i].config(text=f"{variable.get():.1f}")

        for i, default in enumerate(initial_values):
            row = tk.Frame(frame, bg=bg)
            row.pack(fill=tk.X, pady=8)

            tk.Label(
                row,
                text=f"K{i+1} – Kryterium {i+1}",
                bg=bg, fg=fg,
                font=("Segoe UI", 10, "bold")
            ).pack(side=tk.LEFT)

            variable = tk.DoubleVar(value=default)
            variables.append(variable)

            value_label = tk.Label(
                row,
                text=f"{default:.1f}",
                bg=bg, fg=self.palette["accent"],
                font=("Segoe UI", 10, "bold"),
                width=5
            )
            value_label.pack(side=tk.RIGHT)
            value_labels.append(value_label)

            scale = tk.Scale(
                frame,
                from_=0.0,
                to=2.0,
                resolution=0.1,
                orient=tk.HORIZONTAL,
                variable=variable,
                command=update_sum,
                bg=bg,
                fg=fg,
                troughcolor=self.palette["border"],
                highlightthickness=0,
                activebackground=self.palette["hover"],
                showvalue=False,
                length=350
            )
            scale.pack(fill=tk.X)

        # Od?wie? sum? i warto?ci od razu po otwarciu okna.
        update_sum()

        def reset_values():
            for variable, value in zip(variables, defaults):
                variable.set(value)
            update_sum()

        def save_weights():
            import json
            import subprocess
            import sys
            from pathlib import Path

            total = sum(v.get() for v in variables)

            if total <= 0:
                messagebox.showwarning(
                    "Nieprawid?owa suma wag",
                    "Suma wag musi by? wi?ksza od zera.",
                    parent=window
                )
                return
            if any(v.get() < 0 for v in variables):
                messagebox.showwarning(
                    "Nieprawid?owe wagi",
                    "Wagi nie mog? by? ujemne.",
                    parent=window
                )
                return

            root_dir = Path(__file__).resolve().parents[1]
            config_path = root_dir / "config" / "iop_config.json"
            script_path = root_dir / "scripts" / "run_iop.py"
            sql_path = root_dir / "scripts" / "create_iop_color_layers.sql"

            old_config = config_path.read_text(encoding="utf-8")
            calculation_done = False

            try:
                config = json.loads(old_config)

                config["weights"] = {
                    "k_land": variables[0].get(),
                    "k_hydro": variables[1].get(),
                    "k_roads": variables[2].get(),
                    "k_buildings": variables[3].get()
                }

                temp_path = config_path.with_suffix(".json.tmp")
                temp_path.write_text(
                    json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8"
                )
                temp_path.replace(config_path)

                self._set_status("IOP: przeliczanie 143 heksagon?w...")
                window.update_idletasks()

                result = subprocess.run(
                    [sys.executable, str(script_path), "--replace"],
                    cwd=str(root_dir),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300
                )

                if result.returncode != 0:
                    raise RuntimeError(
                        "Skrypt run_iop.py zako?czy? si? b??dem:\n\n"
                        + result.stdout[-3000:]
                        + "\n"
                        + result.stderr[-5000:]
                    )

                calculation_done = True

                self._set_status("IOP: aktualizacja warstw kolorystycznych...")
                window.update_idletasks()

                sql_text = sql_path.read_text(encoding="utf-8")

                with self._connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql_text)

                # Zachowaj ustawienia ka?dej istniej?cej warstwy IOP.
                layer._iop_weights = [v.get() for v in variables]

                loaded_iop = [
                    (
                        item.info.schema,
                        item.info.table,
                        item.resolution,
                        item.visible,
                        list(getattr(item, "_iop_weights", defaults))
                    )
                    for item in self.layers
                    if item.info.has_iop
                ]

                # Od?wie? katalog tabel PostGIS.
                if not self.refresh_layer_catalog(show_errors=False):
                    raise RuntimeError(
                        "Nie uda?o si? od?wie?y? katalogu warstw PostGIS."
                    )

                # Ponownie wczytaj dotychczasowe warstwy IOP.
                for schema, table, resolution, was_visible, saved_weights in loaded_iop:
                    info = next(
                        (
                            item for item in self.layer_infos
                            if item.schema == schema and item.table == table
                        ),
                        None
                    )

                    if info is None:
                        continue

                    self._load_layer(
                        info,
                        resolution=resolution,
                        fit=False,
                        show_errors=False
                    )

                    for loaded in self.layers:
                        if (
                            loaded.info.schema == schema
                            and loaded.info.table == table
                            and loaded.resolution == resolution
                        ):
                            loaded.visible = was_visible
                            loaded._iop_weights = list(saved_weights)

                self._refresh_layer_list()
                self.draw_map(fit=False)

                self._set_status(
                    f"IOP przeliczony poprawnie. Suma wag: {total:.1f}."
                )

                messagebox.showinfo(
                    "IOP - zako\u0144czono",
                    "Przeliczanie zako\u0144czone poprawnie.\n\n"
                    "Zaktualizowano:\n"
                    "- grid.iop_test\n"
                    "- grid.iop_color_6\n"
                    "- grid.iop_color_7\n"
                    "- grid.iop_color_8\n\n"
                    "Mapa zosta\u0142a od\u015bwie\u017cona.",
                    parent=window
                )

                window.destroy()

            except Exception as exc:
                if not calculation_done:
                    config_path.write_text(old_config, encoding="utf-8")

                self._set_status("B??d podczas przeliczania IOP.")

                messagebox.showerror(
                    "B??d przeliczania IOP",
                    str(exc),
                    parent=window
                )

        buttons = tk.Frame(frame, bg=bg)
        buttons.pack(fill=tk.X, pady=(10, 0))

        tk.Button(
            buttons,
            text="Przywróć domyślne",
            command=reset_values,
            bg=self.palette["panel"], fg=fg,
            activebackground=self.palette["hover"],
            activeforeground=fg,
            relief=tk.FLAT,
            padx=10, pady=8
        ).pack(side=tk.LEFT)

        tk.Button(
            buttons,
            text="Zapisz",
            command=save_weights,
            bg="#287a52", fg="white",
            activebackground="#359568",
            activeforeground="white",
            relief=tk.FLAT,
            padx=18, pady=8
        ).pack(side=tk.RIGHT, padx=(0, 8))

        tk.Button(
            buttons,
            text="Zamknij",
            command=window.destroy,
            bg="#3b526b", fg="white",
            activebackground="#506b88",
            activeforeground="white",
            relief=tk.FLAT,
            padx=18, pady=8
        ).pack(side=tk.RIGHT)

        tk.Label(
            frame,
            text="Zmiana wag jest obecnie podglądem. Przeliczanie danych dodamy w kolejnym etapie.",
            bg=bg, fg=muted,
            font=("Segoe UI", 8),
            wraplength=350,
            justify=tk.LEFT
        ).pack(anchor="w", pady=(15, 0))

        window.grab_set()
        window.focus_force()
    def zoom_to_full_extent(self):
        bounds = []

        for layer in self.layers:
            if not getattr(layer, "visible", True):
                continue

            for geom in getattr(layer, "geometries", []):
                if geom is None or geom.is_empty:
                    continue

                bounds.append(geom.bounds)

        if not bounds:
            self._set_status("Brak widocznych warstw.")
            return

        minx = min(b[0] for b in bounds)
        miny = min(b[1] for b in bounds)
        maxx = max(b[2] for b in bounds)
        maxy = max(b[3] for b in bounds)

        padding = max(maxx - minx, maxy - miny, 0.000001) * 0.04

        self.ax.set_xlim(minx - padding, maxx + padding)
        self.ax.set_ylim(miny - padding, maxy + padding)
        self.ax.set_aspect("auto")

        self.canvas.draw_idle()
        self._set_status("Dopasowano widok do wszystkich widocznych warstw.")
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

        if layer.source_path is not None:
            details.insert(1, ("Plik", str(layer.source_path)))

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

    def _searchable_attributes(self, info):
        with closing(self._connect()) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, udt_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                  AND udt_name NOT IN ('geometry', 'geography', 'raster')
                ORDER BY ordinal_position
                """,
                (info.schema, info.table),
            )
            return dict(cur.fetchall())

    def _attribute_values(self, layer, field):
        if layer.attribute_rows is not None:
            return sorted({
                str(row[field]) for row in layer.attribute_rows
                if row.get(field) is not None and row[field] != ""
            })[:201]

        info = layer.info
        query = sql.SQL("""
            SELECT DISTINCT {field}::text
            FROM {schema}.{table}
            WHERE {field} IS NOT NULL AND {field}::text <> ''
              AND {geom} IS NOT NULL AND NOT ST_IsEmpty({geom})
        """).format(
            field=sql.Identifier(field), schema=sql.Identifier(info.schema),
            table=sql.Identifier(info.table), geom=sql.Identifier(info.geom_column),
        )
        params = []
        if layer.resolution is not None and info.has_resolution:
            query += sql.SQL(" AND resolution = %s")
            params.append(layer.resolution)
        with closing(self._connect()) as conn, conn.cursor() as cur:
            cur.execute(query + sql.SQL(" ORDER BY 1 LIMIT 201"), params)
            return [value for (value,) in cur.fetchall()]

    def _find_attribute_matches(self, layer, field, field_type, term):
        info = layer.info
        numeric = field_type in {"int2", "int4", "int8", "numeric", "float4", "float8"}
        if numeric:
            try:
                number = Decimal(term.replace(",", "."))
                if not number.is_finite():
                    raise InvalidOperation
            except InvalidOperation:
                raise ValueError("Wpisz poprawną wartość liczbową.") from None

        if layer.attribute_rows is not None:
            matches = []
            for row, geometry in zip(layer.attribute_rows, layer.geometries):
                value = row.get(field)
                if value is None:
                    continue
                matched = Decimal(str(value)) == number if numeric else term.casefold() in str(value).casefold()
                if matched:
                    matches.append((str(value), geometry))
                    if len(matches) == 101:
                        break
            return matches

        column = sql.Identifier(field)
        if numeric:
            predicate = sql.SQL("{} = %s").format(column)
            params = [number]
        else:
            predicate = sql.SQL("strpos(lower({}::text), lower(%s)) > 0").format(column)
            params = [term]

        query = sql.SQL("""
            SELECT {column}::text, ST_AsBinary(ST_Transform({geom}, 4326))
            FROM {schema}.{table}
            WHERE {predicate} AND {geom} IS NOT NULL AND NOT ST_IsEmpty({geom})
        """).format(
            column=column, geom=sql.Identifier(info.geom_column),
            schema=sql.Identifier(info.schema), table=sql.Identifier(info.table),
            predicate=predicate,
        )
        if layer.resolution is not None and info.has_resolution:
            query += sql.SQL(" AND resolution = %s")
            params.append(layer.resolution)

        with closing(self._connect()) as conn, conn.cursor() as cur:
            cur.execute(query + sql.SQL(" LIMIT 101"), params)
            return [(label, wkb.loads(bytes(geometry))) for label, geometry in cur.fetchall()]

    def show_attribute_search(self):
        index = self._selected_layer_index()
        if index is None:
            messagebox.showinfo("Szukaj po atrybucie", "Najpierw zaznacz warstwę.", parent=self.root)
            return

        layer = self.layers[index]
        try:
            fields = layer.attribute_types
            if fields is None:
                fields = self._searchable_attributes(layer.info)
        except psycopg2.Error as error:
            messagebox.showerror("Nie udało się pobrać atrybutów", str(error), parent=self.root)
            return
        if not fields:
            messagebox.showinfo("Szukaj po atrybucie", "Warstwa nie ma pól do wyszukania.", parent=self.root)
            return

        window = tk.Toplevel(self.root)
        window.title("Szukaj po atrybucie")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(4, weight=1)

        ttk.Label(frame, text=layer.display_name).grid(row=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(frame, text="Atrybut").grid(row=1, column=0, sticky="w", padx=(0, 8))
        field = ttk.Combobox(frame, values=list(fields), state="readonly", width=30)
        field.grid(row=1, column=1, sticky="ew")
        field.current(list(fields).index("iop") if "iop" in fields else 0)
        ttk.Label(frame, text="Wartość").grid(row=2, column=0, sticky="w", padx=(0, 8))
        term = ttk.Combobox(frame, state="normal")
        term.grid(row=2, column=1, sticky="ew")
        status = ttk.Label(frame, text="Wybierz pole i wpisz szukaną wartość.")
        status.grid(row=3, columnspan=3, sticky="w", pady=(8, 4))

        results = ttk.Treeview(frame, columns=("value",), show="headings", height=8, selectmode="browse")
        results.heading("value", text="Wyniki")
        results.grid(row=4, column=0, columnspan=2, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=results.yview)
        scroll.grid(row=4, column=2, sticky="ns")
        results.configure(yscrollcommand=scroll.set)
        matches = []

        suggestions = {}

        def load_values():
            attribute = field.get()
            if attribute not in suggestions:
                try:
                    suggestions[attribute] = self._attribute_values(layer, attribute)
                except psycopg2.Error:
                    term.configure(values=())
                    status.configure(text="Nie udało się pobrać listy. Możesz wpisać wartość.")
                    return
            values = suggestions[attribute]
            term.configure(values=values[:200])
            if len(values) > 200:
                message = "Lista pokazuje 200 wartości. Możesz wpisać inną."
            elif values:
                message = "Wybierz wartość z listy lub wpisz ją ręcznie."
            else:
                message = "To pole nie ma dostępnych wartości."
            status.configure(text=message)

        def change_attribute(_event=None):
            term.set("")
            matches.clear()
            results.delete(*results.get_children())
            load_values()

        def search(_event=None):
            matches.clear()
            results.delete(*results.get_children())
            value = term.get().strip()
            if not value:
                status.configure(text="Wpisz szukaną wartość.")
                return
            try:
                found = self._find_attribute_matches(layer, field.get(), fields[field.get()], value)
            except (ValueError, psycopg2.Error) as error:
                status.configure(text="Wyszukiwanie nie powiodło się.")
                messagebox.showerror("Szukaj po atrybucie", str(error), parent=window)
                return
            matches.extend(found[:100])
            for index, (label, _) in enumerate(matches):
                results.insert("", tk.END, iid=str(index), values=(label,))
            if matches:
                results.selection_set("0")
                results.focus("0")
            if len(found) > 100:
                message = "Pokazano pierwsze 100 wyników. Zawęź wyszukiwanie."
            else:
                message = f"Liczba wyników: {len(matches)}" if matches else "Brak wyników."
            status.configure(text=message)

        def show_result(_event=None):
            selected = results.selection()
            if not selected:
                return
            if not any(current is layer for current in self.layers):
                status.configure(text="Warstwa została zmieniona. Otwórz wyszukiwanie ponownie.")
                return
            _, geometry = matches[int(selected[0])]
            layer.visible = True
            self.attribute_highlight = (layer, geometry)
            minx, miny, maxx, maxy = geometry.bounds
            padding = max(maxx - minx, maxy - miny, 0.001) * 0.15
            self.ax.set_xlim(minx - padding, maxx + padding)
            self.ax.set_ylim(miny - padding, maxy + padding)
            self._refresh_layer_list()
            self.draw_map()
            self.root.lift()
            window.lift()

        ttk.Button(frame, text="Szukaj", command=search).grid(row=2, column=2, padx=(6, 0))
        actions = ttk.Frame(frame)
        actions.grid(row=5, columnspan=3, sticky="e", pady=(8, 0))
        ttk.Button(actions, text="Pokaż na mapie", command=show_result).pack(side=tk.LEFT, padx=5)
        ttk.Button(actions, text="Zamknij", command=window.destroy).pack(side=tk.LEFT)
        field.bind("<<ComboboxSelected>>", change_attribute)
        load_values()
        term.bind("<Return>", search)
        results.bind("<Double-1>", show_result)
        window.bind("<Escape>", lambda event: window.destroy())
        term.focus_set()

    def _draw_attribute_highlight(self, geometry, zorder):
        if isinstance(geometry, Polygon):
            geometry = geometry.boundary
        if isinstance(geometry, LineString):
            self.ax.plot(*geometry.xy, color="#ffeb69", linewidth=2.5, zorder=zorder)
        elif isinstance(geometry, Point):
            self.ax.scatter(geometry.x, geometry.y, s=90, facecolors="none",
                            edgecolors="#ffeb69", linewidths=2, zorder=zorder)
        elif hasattr(geometry, "geoms"):
            for part in geometry.geoms:
                self._draw_attribute_highlight(part, zorder)

    def toggle_selected_layer(self, _event=None):
        idx = self._selected_layer_index()
        if idx is None or idx >= len(self.layers):
            return

        self.layers[idx].visible = not self.layers[idx].visible
        self._refresh_layer_list()

        self.draw_map(fit=False)

    def remove_selected_layer(self):
        idx = self._selected_layer_index()
        if idx is None:
            messagebox.showinfo("Warstwy", "Najpierw zaznacz warstwę na liĹ›cie.", parent=self.root)
            return
        removed = self.layers.pop(idx)
        if removed is self._point_target:
            self._cancel_point_capture()

        # Usuni?cie wag przypisanych do usuwanej warstwy.
        # Ponowne dodanie warstwy rozpocznie si? od ustawie? domy?lnych.
        weights_cache = getattr(self, "_iop_weights_by_layer", None)
        if weights_cache is not None:
            layer_key = (
                removed.info.full_name,
                removed.resolution
            )
            weights_cache.pop(layer_key, None)

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
        self._measure_lines = None
        self.ax.set_axis_off()
        self.ax.set_position([0, 0, 1, 1])
        self.ax.tick_params(axis="both", which="both", labelbottom=False, labelleft=False, bottom=False, left=False)
        self.ax.set_facecolor(self.palette["map"])
        visible_layers = [layer for layer in self.layers if layer.visible]
        if not visible_layers:
            self.ax.text(0.5, 0.5, "Brak widocznych warstw\nKliknij „Dodaj warstwę”",
                         transform=self.ax.transAxes, ha="center", va="center",
                         fontsize=14, color=self.palette["muted"])
            self.ax.set_xlabel("")
            self.ax.set_ylabel("")
            self.ax.grid(True, linewidth=0.5, color=self.palette["grid"], alpha=0.65)
            self.ax.set_aspect("auto")
            self.ax.margins(x=0.005, y=0.005)
            self._draw_measurement()
            self.canvas.draw_idle()
            return

        for stack_index, layer in enumerate(reversed(visible_layers)):
            for feature_index, geom in enumerate(layer.geometries):
                feature_color = (
                    layer.feature_colors[feature_index]
                    if layer.feature_colors is not None
                    and feature_index < len(layer.feature_colors)
                    else None
                )
                self._draw_geometry(geom, layer, feature_color, stack_index * 5)

        if self.attribute_highlight is not None:
            layer, geometry = self.attribute_highlight
            if any(current is layer for current in visible_layers):
                self._draw_attribute_highlight(geometry, 5 * len(visible_layers) + 5)

        self.ax.set_xlabel("")
        self.ax.set_ylabel("")
        self.ax.grid(True, linewidth=0.5, color=self.palette["grid"], alpha=0.65)
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
        self._draw_measurement()
        self.canvas.draw_idle()

    def _draw_geometry(self, geom, layer, feature_color=None, base_zorder=0):
        if geom is None or geom.is_empty:
            return

        draw_color = feature_color or layer.color

        if isinstance(geom, Polygon):
            x, y = geom.exterior.xy

            if layer.info.table == "powiat":
                self.ax.plot(x, y, color=draw_color, linewidth=2.0, zorder=base_zorder + 5)
                for ring in geom.interiors:
                    ix, iy = ring.xy
                    self.ax.plot(ix, iy, color=draw_color, linewidth=1.0, zorder=base_zorder + 5)

            elif feature_color is not None:
                # Warstwa IOP: mocne wypełnienie osobnym kolorem każdego heksagonu.
                self.ax.fill(
                    x, y,
                    facecolor=feature_color,
                    edgecolor="#45474d",
                    linewidth=0.45,
                    alpha=0.92,
                    zorder=base_zorder + 4,
                )
                for ring in geom.interiors:
                    ix, iy = ring.xy
                    self.ax.fill(
                        ix, iy,
                        facecolor=self.palette["map"],
                        edgecolor="#45474d",
                        linewidth=0.35,
                        zorder=base_zorder + 4,
                    )

            else:
                # Dotychczasowy wygląd zwykłych warstw pozostaje bez zmian.
                self.ax.fill(
                    x, y,
                    facecolor=layer.color,
                    edgecolor=layer.color,
                    linewidth=0.65,
                    alpha=0.20,
                    zorder=base_zorder + 2,
                )
                for ring in geom.interiors:
                    ix, iy = ring.xy
                    self.ax.fill(
                        ix, iy,
                        facecolor=self.palette["map"],
                        edgecolor=layer.color,
                        linewidth=0.35,
                        zorder=base_zorder + 2,
                    )

        elif isinstance(geom, MultiPolygon):
            for part in geom.geoms:
                self._draw_geometry(part, layer, feature_color, base_zorder)

        elif isinstance(geom, (LineString, MultiLineString)):
            if isinstance(geom, LineString):
                x, y = geom.xy
                self.ax.plot(
                    x, y, color=draw_color,
                    linewidth=0.8, alpha=0.9, zorder=base_zorder + 3
                )
            else:
                for part in geom.geoms:
                    self._draw_geometry(part, layer, feature_color, base_zorder)

        elif isinstance(geom, Point):
            self.ax.scatter([geom.x], [geom.y], s=9, color=draw_color, zorder=base_zorder + 4)

        elif isinstance(geom, MultiPoint):
            self.ax.scatter(
                [point.x for point in geom.geoms],
                [point.y for point in geom.geoms],
                s=9, color=draw_color, zorder=base_zorder + 4
            )

        elif isinstance(geom, GeometryCollection):
            for part in geom.geoms:
                self._draw_geometry(part, layer, feature_color, base_zorder)

    def about(self):
        messagebox.showinfo(
            "O programie",
            "PSIP GIS â€” demonstracyjny klient GIS w Tkinterze.\n"
            "Dane przestrzenne s\u0105 odczytywane z PostGIS; aplikacja nie modyfikuje tabel \u017ar\u00f3d\u0142owych.\\n"
            f"Limit odczytu na jedn\u0105 warstw\u0119: {MAX_FEATURES} obiekt\u00f3w.",
            parent=self.root
        )


def settings_directory():
    return Path(os.getenv("LOCALAPPDATA") or Path.home()) / "PSIP GIS"


def password_digest(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)


def login(root):
    account_path = settings_directory() / "account.json"
    try:
        account = json.loads(account_path.read_text(encoding="utf-8"))
        stored_login = account["login"]
        salt = bytes.fromhex(account["salt"])
        expected = bytes.fromhex(account["digest"])
        if not stored_login or not isinstance(stored_login, str) or len(salt) != 16 or len(expected) != 64:
            raise ValueError
    except FileNotFoundError:
        account = None
    except (OSError, ValueError, KeyError, TypeError):
        messagebox.showerror(
            "PSIP GIS", "Nie można odczytać lokalnego konta. Sprawdź plik ustawień.",
            parent=root,
        )
        return False

    setup = account is None
    root.title("PSIP GIS — Utwórz konto" if setup else "PSIP GIS — Logowanie")
    root.resizable(False, False)
    style = ttk.Style(root)
    style.theme_use("clam")
    try:
        theme = (settings_directory() / "theme.txt").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        theme = "dark"
    palette = THEMES.get(theme, THEMES["dark"])
    root.configure(bg=palette["bg"])
    style.configure(".", background=palette["bg"], foreground=palette["text"])
    style.configure("TEntry", fieldbackground=palette["field"], foreground=palette["text"],
                    insertcolor=palette["text"])
    style.configure("TButton", background=palette["panel"], foreground=palette["text"])
    style.map("TButton", background=[("active", palette["hover"])])
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill=tk.BOTH, expand=True)
    frame.columnconfigure(1, weight=1)

    title = "Utwórz lokalne konto PSIP GIS" if setup else "Zaloguj się do PSIP GIS"
    ttk.Label(frame, text=title, font=("Segoe UI", 12, "bold")).grid(
        row=0, columnspan=2, sticky="w", pady=(0, 12)
    )
    login_var = tk.StringVar(master=root)
    password = tk.StringVar(master=root)
    confirmation = tk.StringVar(master=root)
    error = tk.StringVar(master=root)
    fields = [("Login", login_var, ""), ("Hasło", password, "•")]
    if setup:
        fields.append(("Powtórz hasło", confirmation, "•"))
    for row, (label, variable, character) in enumerate(fields, start=1):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
        entry = ttk.Entry(frame, textvariable=variable, show=character, width=30)
        entry.grid(row=row, column=1, sticky="ew", pady=3)
        if row == 1:
            entry.focus_set()

    ttk.Label(frame, textvariable=error, foreground=palette["error"], wraplength=300).grid(
        row=4, columnspan=2, sticky="w", pady=(6, 0)
    )
    authenticated = False

    def submit(_event=None):
        nonlocal authenticated
        username = login_var.get().strip()
        secret = password.get()
        if setup:
            if not username or len(secret) < 8:
                error.set("Wpisz login i hasło mające co najmniej 8 znaków.")
                return
            if secret != confirmation.get():
                error.set("Wpisane hasła się różnią.")
                return
            new_salt = secrets.token_bytes(16)
            record = {
                "login": username, "salt": new_salt.hex(),
                "digest": password_digest(secret, new_salt).hex(),
            }
            try:
                account_path.parent.mkdir(parents=True, exist_ok=True)
                with account_path.open("x", encoding="utf-8") as stream:
                    json.dump(record, stream)
            except OSError:
                error.set("Nie udało się zapisać konta. Jeśli już istnieje, uruchom aplikację ponownie.")
                return
        elif not (hmac.compare_digest(password_digest(secret, salt), expected) and username == stored_login):
            error.set("Nieprawidłowy login lub hasło.")
            password.set("")
            return
        authenticated = True
        root.destroy()

    ttk.Button(frame, text="Utwórz konto" if setup else "Zaloguj", command=submit).grid(
        row=5, column=1, sticky="e", pady=(10, 0)
    )
    root.bind("<Return>", submit)
    root.bind("<Escape>", lambda event: root.destroy())
    root.mainloop()
    return authenticated


def main():
    while True:
        login_window = tk.Tk()
        if not login(login_window):
            return

        root = tk.Tk()
        logged_out = False

        def logout():
            nonlocal logged_out
            logged_out = True
            root.quit()

        PSIPGISApp(root, on_logout=logout)
        # Leave the interpreter intact until the event loop returns, then
        # discard the map, dialogs, and pending callbacks with this session.
        root.protocol("WM_DELETE_WINDOW", root.quit)
        root.mainloop()
        for callback in root.tk.splitlist(root.tk.call("after", "info")):
            root.tk.call("after", "cancel", callback)
        root.destroy()
        if not logged_out:
            return


if __name__ == "__main__":
    main()
