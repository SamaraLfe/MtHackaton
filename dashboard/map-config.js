// Новая подложка Esri World Street Map: отдельный provider, без API key.
// Ключи картографических сервисов в коде не хранятся.
window.TRANSIT_MAP = {
  tileUrl: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
  attribution: 'Tiles &copy; Esri — Source: Esri, HERE, Garmin, USGS, NGA, EPA, USDA, NPS',
  maxZoom: 19,
  minZoom: 3
};
