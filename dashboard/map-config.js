// CARTO light basemap is keyless and has no branded Leaflet prefix or flag icon.
// Provider attribution remains visible as required by the tile licence.
window.TRANSIT_MAP = {
  tileUrl: 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
  subdomains: 'abcd',
  attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
  maxZoom: 20,
  minZoom: 3
};
