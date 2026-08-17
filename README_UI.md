# Oberfläche

Die Oberfläche ist deutsch und hat zwei Ebenen:

## Übersicht

Die Übersicht ist für den täglichen Betrieb gedacht. Sie zeigt 24 Streams in einem gut lesbaren Raster mit:

- Name
- kurzer Status
- Stream-Link
- Bearbeiten
- Neu starten
- Herunterfahren

Technische Details und Update-/Recovery-Meldungen bleiben aus dieser Ansicht heraus.

**Bearbeiten** öffnet einen Dialog. `Abbrechen`, `Schließen` und `Esc` schließen ihn immer – auch wenn noch keine Stream-URL eingetragen ist. Eine leere URL ist erlaubt und bedeutet „kein Stream konfiguriert“.

## Adminbereich

Der Adminbereich wird erst nach einer Warnung geöffnet. Er hat bewusst mehr Abstand und stärkeren Kontrast zwischen Seitenhintergrund, Stream-Karten, Technikbereich und globalen Aktionen.

Zusätzlich verfügbar sind u. a.:

- Start / Stop / Logs / Statusprüfung
- vollständige SSH-/Qualitätskonfiguration
- VLC-/Streamlink-Updates
- OverlayFS-Recovery
- globale Wartungsaktionen

## Browser-Titel und Favicon

Titel und Favicon zeigen das Verhältnis der erreichbaren Pis mit laufendem Stream-Wächter zur Gesamtzahl, z. B.:

```text
00x/24x Streaming Setup
17x/24x Streaming Setup
24x/24x Streaming Setup
```

Der Projektname selbst bleibt **Streaming Setup**.
