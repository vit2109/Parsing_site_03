// Run with: node tests/leaflet_scan_check.js path/to/extracted_script.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const extract = eval(fs.readFileSync(process.argv[2], 'utf8'));
const nativeSetTimeout = global.setTimeout;

async function check(initialZoom, mapDiscoverable = true, crowded = false, overlapping = false,
  sameNumber = false, denseUnique = false, bounded = false, mixedLabels = false,
  highStart = false) {
  let zoomInMoves = 0;
  let zoomInWaits = 0;
  let awaitingZoomInPause = false;
  let minimumVisitedZoom = initialZoom;
  let maximumVisitedZoom = initialZoom;
  let offRowMoves = 0;
  let farMoves = 0;
  const moves = [];
  global.setTimeout = (callback, delay, ...args) => {
    if (awaitingZoomInPause) {
      assert.equal(delay, 1500, 'pause after zooming in');
      zoomInWaits++;
      awaitingZoomInPause = false;
    }
    return nativeSetTimeout(callback, Math.min(delay, 1), ...args);
  };
  const points = mixedLabels ? [
    {lat: 50, lng: 0, number: 101},
    {lat: 50, lng: 0.5, number: 102},
    {lat: 50, lng: 1, number: 103},
  ] : bounded ? [
    ...Array.from({length: 500}, (_, i) => ({
      lat: 50 + (i % 5) * 0.01, lng: (i % 10) * 0.02, number: 1000 + i,
    })),
    ...Array.from({length: 500}, (_, i) => ({
      lat: 50 + (i % 5) * 0.01, lng: 12 + (i % 10) * 0.02, number: 2000 + i,
    })),
    {lat: 5, lng: 60, number: 9999},
  ] : denseUnique ? Array.from({length: 9}, (_, i) => (
    {lat: 50, lng: i * 0.015, number: 101 + i}
  )) : sameNumber ? Array.from({length: 30}, (_, i) => (
    {lat: 50, lng: 0, number: 101, id: i + 1}
  )) : overlapping ? Array.from({length: 9}, (_, i) => (
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
      moves.push([lat, lng, zoom]);
      minimumVisitedZoom = Math.min(minimumVisitedZoom, zoom);
      maximumVisitedZoom = Math.max(maximumVisitedZoom, zoom);
      if (Math.abs(lat - 50) > 0.01) offRowMoves++;
      if (lat < 45 || lat > 55 || lng < -6 || lng > 18) farMoves++;
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
  function element(point, number, origin = false, plain = false) {
    const p = map.latLngToContainerPoint([point.lat, point.lng]);
    const type = 'map-incident_aircraft-drone';
    return {
      classList: number ? ['leaflet-marker-icon', 'map-incident-operator-number']
        : ['leaflet-marker-icon', 'map-incident', type],
      dataset: overlapping ? {} : {incidentId: String(point.id || point.number)},
      id: '', textContent: number ? String(origin ? point.number + 1000 : point.number) : '',
      style: {marginTop: number ? (plain ? '' : origin ? '-9px' : '-19px') : '-10px'},
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
              .flatMap((p) => mixedLabels && p.number === 102
                ? [element(p, true, false, true)]
                : [element(p, true, true), element(p, true)]) : [];
      return [];
    },
  };
  global.getComputedStyle = () => ({backgroundColor: 'red'});
  if (mixedLabels) {
    const raw = await extract({
      mode: 'latlng', mapContainer: '.leaflet-container',
      dynamic: '.leaflet-marker-icon.map-incident', static: '.static',
      staticObjects: [], scanNumbers: false,
    });
    assert.deepEqual(raw.dynamics.map((item) => item.index), [101, 102, 103],
      'an unstyled label on one marker survives current labels on other markers');
    return;
  }
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
    staticObjects: bounded ? [{lat: 50, lng: 0}, {lat: 50, lng: 12}] : [],
    scanNumbers: true, captureNumbered: true,
    scanMaxZoom: bounded || highStart ? 8 : 10, scanRadiusKm: 350,
  });
  if (!mapDiscoverable) {
    assert.equal(result.scan.status, 'no_leaflet_instance');
    assert.equal(result.unnumbered_dynamics,
      document.querySelectorAll('.leaflet-marker-icon.map-incident').length);
    return;
  }
  if (highStart) {
    assert.equal(minimumVisitedZoom, 4, 'search must begin at overview zoom');
    assert.deepEqual(moves.filter((move) => move[2] > 8), [[50, 0, initialZoom]],
      'scan zoom ceiling must apply even when the original view is closer');
    assert.ok(moves.every((move, index) => index === 0 ||
      moves[index - 1][2] < 8 || move[2] < 8 ||
      Math.abs(move[1] - moves[index - 1][1]) <= 1),
    'distant groups must never be visited by panning at high zoom');
  }
  if (bounded) {
    assert.equal(result.dynamics.length, 1000, 'remote outlier must be excluded');
    assert.equal(farMoves, 0, 'scan must stay near configured static areas');
    assert.ok(maximumVisitedZoom <= 8, 'scan must obey configured zoom ceiling');
    assert.ok(result.scan.views <= 34, 'scan must have a finite move budget');
    assert.deepEqual(map.center, {lat: 50, lng: 0});
    assert.equal(map.zoom, initialZoom);
    return;
  }
  if (sameNumber) {
    assert.equal(result.dynamics.length, 30);
    assert.equal(result.dynamics.filter((item) => item.index > 0).length, 1);
    assert.equal(zoomInMoves, 2, 'one dense group plus original-zoom restoration');
    assert.deepEqual(map.center, {lat: 50, lng: 0});
    assert.equal(map.zoom, initialZoom);
    return;
  }
  if (denseUnique) {
    assert.equal(result.dynamics.length, 9);
    assert.equal(result.unnumbered_dynamics, 0);
    assert.equal(result.scan.status, 'complete');
    assert.equal(zoomInMoves, 2, 'one dense view plus original-zoom restoration');
    assert.deepEqual(map.center, {lat: 50, lng: 0});
    assert.equal(map.zoom, initialZoom);
    return;
  }
  assert.deepEqual(result.dynamics.map((item) => item.index).sort((a, b) => a - b),
    points.map((point) => point.number), JSON.stringify({scan: result.scan, moves}));
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
  await check(6, true, false, false, true); // 30 копий одного номера — один общий вид
  await check(6, true, false, false, false, true); // 9 разных номеров в одной группе
  await check(6, true, false, false, false, false, true); // 1000 маркеров и далёкий выброс
  await check(8, true, false, false, false, false, false, true); // подписи с разными стилями
  await check(10, true, false, false, false, false, false, false, true); // старт с сильного приближения
  console.log('Leaflet scan: both zoom cases passed');
})().catch((error) => {console.error(error); process.exitCode = 1;});
