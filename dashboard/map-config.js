// Standard OpenStreetMap raster tiles are keyless and avoid the CARTO API-key
// watermark that obscured the operations map. Attribution stays visible.
window.TRANSIT_MAP = {
  tileUrl: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  attribution: '&copy; OpenStreetMap contributors',
  maxZoom: 19,
  minZoom: 3
};
