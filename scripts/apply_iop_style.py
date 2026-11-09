from qgis.core import (
    QgsDataSourceUri, QgsVectorLayer,
    QgsGraduatedSymbolRenderer, QgsRendererRange,
    QgsFillSymbol
)
from qgis.PyQt.QtGui import QColor
from pathlib import Path
from qgis.utils import iface

log = Path(r"C:\Users\janmi\PycharmProjects\PSIP_JM_L1\outputs\iop_style_log.txt")

try:
    uri = QgsDataSourceUri()
    uri.setConnection("localhost", "5434", "psip", "psip", "psip")
    uri.setDataSource("grid", "iop_test", "geom", "", "h3_id")

    layer = QgsVectorLayer(uri.uri(False), "IOP â€“ przejezdnoĹ›Ä‡", "postgres")

    if not layer.isValid():
        raise Exception("Nie udaĹ‚o siÄ™ otworzyÄ‡ grid.iop_test")

    idx = layer.fields().indexFromName("iop")
    if idx < 0:
        raise Exception("Brak kolumny iop")

    minimum = float(layer.minimumValue(idx))
    maximum = float(layer.maximumValue(idx))

    if minimum >= 0 or maximum <= 0:
        raise Exception("Zakres IOP nie obejmuje zera")

    bounds = [
        minimum,
        minimum * 2 / 3,
        minimum / 3,
        0.0,
        maximum / 3,
        maximum * 2 / 3,
        maximum
    ]

    colors = [
        "#D73027",
        "#EF9A91",
        "#F8D8D5",
        "#FFFFFF",
        "#D8EBDD",
        "#91C9A0",
        "#1A9850"
    ]

    ranges = []
    for i in range(6):
        symbol = QgsFillSymbol.createSimple({
            "color": colors[i],
            "outline_color": "75,75,75,180",
            "outline_width": "0.15"
        })
        ranges.append(QgsRendererRange(
            bounds[i], bounds[i+1], symbol,
            f"{bounds[i]:.4f} â€“ {bounds[i+1]:.4f}"
        ))

    renderer = QgsGraduatedSymbolRenderer("iop", ranges)
    layer.setRenderer(renderer)

    result = layer.saveStyleToDatabase(
        "IOP przejezdnoĹ›Ä‡",
        "DomyĹ›lny styl IOP czerwony-biaĹ‚y-zielony",
        True,
        ""
    )

    iface.addMapLayer(layer)

    log.write_text(
        f"Zapis stylu: {result}\n"
        f"Zakres: {minimum} do {maximum}\n"
        f"Liczba klas: {len(ranges)}\n",
        encoding="utf-8"
    )

except Exception as e:
    log.write_text("BLAD: " + str(e), encoding="utf-8")
    raise
