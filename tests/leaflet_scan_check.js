// Run with: node tests/leaflet_scan_check.js path/to/extracted_script.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const extract = eval(fs.readFileSync(process.argv[2], 'utf8'));
const nativeSetTimeout = global.setTimeout;

async function check(initialZoom, mapDiscoverable = true, crowded = false, overlapping = false) {
  let zoomInMoves = 0;
  let zoomInWaits = 0;
  let awaitingZoomInPause = false;
  let zoomInTimers = 0;
  let minimumVisitedZoom = initialZoom;
  let offRowMoves = 0;
  global.setTimeout = (callback, delay, ...args) => {
    if (awaitingZoomInPause && ++zoomInTimers >= 2) {
      assert.equal(delay, 1500, 'pause after zooming in');
      zoomInWaits++;
      awaitingZoomInPause = false;
      zoomInTimers = 0;
    }
    return nativeSetTimeout(callback, Math.min(delay, 1), ...args);
  };
  const points = overlapping ? Array.from({length: 9}, (_, i) => (
    {lat: 50, lng: 0, number: 101 + i}
  )) : [
    ...Array.from({length: 12}, (_, i) => ({lat: 50, lng: i * 0.5, number: 101 + i})),
    {lat: 50, lng: 12, number: 113}, // В исходном HTML и центральном обзорном окне отсутствует.
  ];
  const listeners = new Set();
  const map = {
    zoom: initialZoom, center: {lat: 50, lng: 0},
    getZoom() { return this.zoom; },
    getCenter() { return {...this.center}; },
    getMinZoom() { return 4; },
    getMaxZoom() { return 10; },
    on(_event, callback) { listeners.add(callback); },
    off(_event, callback) { listeners.delete(callback); },
    setView([lat, lng], zoom) {
      minimumVisitedZoom = Math.min(minimumVisitedZoom, zoom);
      if (Math.abs(lat - 50) > 0.01) offRowMoves++;
      if (zoom > this.zoom) {
        zoomInMoves++;
        awaitingZoomInPause = true;
      }
      this.center = {lat, lng}; this.zoom = zoom;
      for (const callback of [...listeners]) callback();
    },
    latLngToContainerPoint([lat, lng]) {
      const scale = 100 * 2 ** (this.zoom - 6);
      return {x: 400 + (lng - this.center.lng) * scale, y: 300 + (this.center.lat - lat) * scale};
    },
    containerPointToLatLng([x, y]) {
      const scale = 100 * 2 ** (this.zoom - 6);
      return {lat: this.center.lat - (y - 300) / scale, lng: this.center.lng + (x - 400) / scale};
    },
  };
  const parent = {parentElement: null};
  if (mapDiscoverable) {
    parent.__vueParentComponent = {setupState: {leaflet: {value: {instance: map}}}};
  }
  const mapEl = {
    parentElement: parent,
    getBoundingClientRect: () => ({left: 0, top: 0, width: 800, height: 600}),
    querySelector: () => null,
  };
  function element(point, number, origin = false) {
    const p = map.latLngToContainerPoint([point.lat, point.lng]);
    const type = 'map-incident_aircraft-drone';
    return {
      classList: number ? ['leaflet-marker-icon', 'map-incident-operator-number']
        : ['leaflet-marker-icon', 'map-incident', type],
      dataset: overlapping ? {} : {incidentId: String(point.number)},
      id: '', textContent: number ? String(origin ? point.number + 1000 : point.number) : '',
      style: {marginTop: number ? (origin ? '-9px' : '-19px') : '-10px'},
      getBoundingClientRect: () => ({left: p.x - 10, top: p.y - 10, width: 20, height: 20}),
      getAttribute: (name) => name === 'style'
        ? `transform: translate3d(${p.x}px, ${p.y}px, 0px)` : '',
      querySelector: () => null,
    };
  }
  global.window = {innerWidth: 800, innerHeight: 600};
  global.document = {
    querySelector: () => mapEl,
    querySelectorAll(selector) {
      const visible = points.filter((point) => {
        const p = map.latLngToContainerPoint([point.lat, point.lng]);
        return p.x >= 0 && p.x <= 800 && p.y >= 0 && p.y <= 600;
      });
      if (selector === '.leaflet-marker-icon.map-incident') return visible.map((p) => element(p, false));
      if (selector === '.leaflet-marker-icon.map-incident-operator-number')
        return map.zoom >= 8
          ? visible.filter((p) => !crowded || map.zoom >= 10 || p.number % 2 === 0)
              .flatMap((p) => [element(p, true, true), element(p, true)]) : [];
      return [];
    },
  };
  global.getComputedStyle = () => ({backgroundColor: 'red'});
  if (overlapping) {
    const raw = await extract({
      mode: 'latlng', mapContainer: '.leaflet-container',
      dynamic: '.leaflet-marker-icon.map-incident', static: '.static',
      staticObjects: [], scanNumbers: false,
    });
    assert.equal(raw.dynamics.length, 9);
    assert.equal(new Set(raw.dynamics.map((item) => item.fallback_id)).size, 9);
    return;
  }
  const result = await extract({
    mode: 'latlng', mapContainer: '.leaflet-container',
    dynamic: '.leaflet-marker-icon.map-incident', static: '.static',
    staticObjects: [], scanNumbers: true, captureNumbered: true,
  });
  if (!mapDiscoverable) {
    assert.equal(result.scan.status, 'no_leaflet_instance');
    assert.equal(result.unnumbered_dynamics,
      document.querySelectorAll('.leaflet-marker-icon.map-incident').length);
    return;
  }
  assert.deepEqual(result.dynamics.map((item) => item.index).sort((a, b) => a - b),
    points.map((point) => point.number));
  assert.equal(result.unnumbered_dynamics, 0);
  assert.equal(result.scan.status, 'complete');
  assert.ok(result.scan.best_numbered_view?.count > 0);
  assert.ok(result.scan.best_numbered_view.zoom >= 8);
  assert.ok(result.scan.numbered_views?.length > 0);
  assert.deepEqual(map.center, {lat: 50, lng: 0});
  assert.equal(map.zoom, initialZoom);
  assert.ok(minimumVisitedZoom >= 4, 'overview should stay at a scale used by the selected tab');
  assert.equal(offRowMoves, 0, 'scan should follow populated areas, not empty corners');
  assert.ok(zoomInMoves > 0);
  assert.equal(zoomInWaits, zoomInMoves);
}

(async () => {
  await check(6); // все маркеры видны, номера скрыты
  await check(8); // номера видны, часть маркеров за пределами окна
  await check(6, true, true); // скопление: часть подписей появится лишь на следующем масштабе
  await check(6, false); // диагностика, когда карта не найдена
  await check(4, true, false, true); // девять значков с совпадающими координатами
  console.log('Leaflet scan: both zoom cases passed');
})().catch((error) => {console.error(error); process.exitCode = 1;});
