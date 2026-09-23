/* The trips list: the period's trips by day, newest first, their CSV export and a trip's title
   (see web/view/dashboard.py for the callbacks calling these; helpers from summary.js). */
/* global toMs, isNumber, fmt, hhmm, pad, h, dayKey, dayLabel, tripEnd, noUpdate */

/* days shown open, the older ones are folded */
const OPEN_DAYS = 3

function tripsInRange (data, range) {
  if (!data || !range) return []
  return (data.trips || [])
    .filter(t => { const s = toMs(t.start_at) / 1000; return s >= range[0] && s <= range[1] })
    .sort((a, b) => toMs(b.start_at) - toMs(a.start_at))
}

function durationText (minutes) {
  if (!isNumber(minutes)) return '–'
  const total = Math.round(minutes)
  if (total < 60) return `${total} min`
  return `${Math.floor(total / 60)} h ${pad(total % 60)}`
}

function longDay (ms) {
  const date = new Date(ms)
  const options = { weekday: 'long', day: 'numeric', month: 'long' }
  if (date.getFullYear() !== new Date().getFullYear()) options.year = 'numeric'
  return date.toLocaleDateString(undefined, options)
}

function batteryCell (trip) {
  if (!isNumber(trip.start_level) && !isNumber(trip.end_level)) {
    return h('Span', { className: 'psacc-trip-cell psacc-col-battery psacc-muted' }, 'No reading')
  }
  const start = isNumber(trip.start_level) ? trip.start_level : trip.end_level
  const end = isNumber(trip.end_level) ? trip.end_level : start
  const low = Math.max(0, Math.min(start, end))
  const high = Math.min(100, Math.max(start, end))
  const text = start === end ? `${fmt(start, 0)}% · no change` : `${fmt(start, 0)}% → ${fmt(end, 0)}%`
  const mark = high - low < 1
    ? h('Div', { className: 'psacc-level-mark', style: { left: `${low}%` } })
    : h('Div', { className: 'psacc-level-used', style: { left: `${low}%`, width: `${high - low}%` } })
  return h('Div', { className: 'psacc-trip-cell psacc-col-battery' }, [
    h('Div', { className: 'psacc-level' }, mark),
    h('Span', { className: 'num' }, text)
  ])
}

function tripRow (trip) {
  const start = toMs(trip.start_at)
  const time = trip.in_progress
    ? [`${hhmm(start)} – `, h('Span', { className: 'psacc-chip psacc-chip-live' }, 'driving')]
    : `${hhmm(start)} – ${hhmm(tripEnd(trip))}`
  const cell = (className, value) => h('Span', { className: `psacc-trip-cell num ${className}` }, value)
  return h('Div', { className: 'psacc-trip-row' }, [
    cell('psacc-col-time', time),
    cell('psacc-col-distance', `${fmt(trip.distance || 0, 1)} km`),
    cell('psacc-col-duration psacc-muted', durationText(trip.duration)),
    cell('psacc-col-speed psacc-muted', isNumber(trip.speed_average) ? `${fmt(trip.speed_average, 1)} km/h` : '–'),
    batteryCell(trip),
    cell('psacc-col-energy psacc-muted',
      isNumber(trip.consumption_km) && trip.consumption_km > 0 ? `${fmt(trip.consumption_km, 1)} kWh/100` : '–'),
    cell('psacc-col-fuel psacc-muted',
      isNumber(trip.consumption_fuel) && trip.consumption_fuel > 0 ? `${fmt(trip.consumption_fuel, 2)} L` : '–'),
    h('Div', { className: 'psacc-trip-cell psacc-col-action' },
      h('Button', {
        id: { type: 'trip-details', index: trip.id },
        type: 'button',
        className: 'psacc-icon-button psacc-icon-button-flat',
        title: 'Route and altitude',
        'aria-label': `Route and altitude of the ${hhmm(start)} trip`,
        n_clicks: 0
      }, h('Span', { className: 'psacc-icon psacc-icon-route', 'aria-hidden': 'true' })))
  ])
}

const COLUMNS = [['time', 'Time'], ['distance', 'Distance'], ['duration', 'Duration'], ['speed', 'Avg speed'],
  ['battery', 'Battery'], ['energy', 'Energy'], ['fuel', 'Fuel'], ['action', '']]

function dayGroup (dayTrips, index) {
  const distance = dayTrips.reduce((sum, t) => sum + (t.distance || 0), 0)
  const litres = dayTrips.reduce((sum, t) => sum + (isNumber(t.consumption_fuel) ? t.consumption_fuel : 0), 0)
  let totals = `${dayTrips.length} ${dayTrips.length === 1 ? 'trip' : 'trips'} · `
  const parts = [totals, h('Span', { className: 'num psacc-strong' }, `${fmt(distance, 1)} km`)]
  if (litres > 0) parts.push(` · ${fmt(litres, 2)} L fuel`)
  totals = h('Span', { className: 'psacc-card-sub' }, parts)
  return h('Details', { className: 'psacc-card psacc-day', open: index < OPEN_DAYS }, [
    h('Summary', { className: 'psacc-day-head' }, [
      h('Span', { className: 'psacc-day-title' }, longDay(toMs(dayTrips[0].start_at))),
      h('Span', { className: 'psacc-day-totals' }, [totals,
        h('Span', { className: 'psacc-icon psacc-icon-chevron-down psacc-day-chevron', 'aria-hidden': 'true' })])
    ]),
    h('Div', { className: 'psacc-trip-row psacc-trip-columns', 'aria-hidden': 'true' },
      COLUMNS.map(([key, label]) => h('Span', { className: `psacc-trip-cell psacc-col-${key}` }, label))),
    ...dayTrips.map(tripRow)
  ])
}

function psaccTripsList (data, range) { // eslint-disable-line no-unused-vars
  const trips = tripsInRange(data, range)
  if (!trips.length) {
    return [[h('Div', { className: 'psacc-card psacc-empty' }, 'No trip in this period.')], '']
  }
  const days = []
  trips.forEach(trip => {
    const key = dayKey(toMs(trip.start_at))
    if (!days.length || days[days.length - 1].key !== key) days.push({ key, trips: [] })
    days[days.length - 1].trips.push(trip)
  })
  const classes = ['psacc-trips-days']
  if (!trips.some(t => isNumber(t.consumption_fuel) && t.consumption_fuel > 0)) classes.push('psacc-no-fuel')
  if (!trips.some(t => isNumber(t.start_level) || isNumber(t.end_level))) classes.push('psacc-no-battery')
  const distance = trips.reduce((sum, t) => sum + (t.distance || 0), 0)
  const count = `${trips.length} ${trips.length === 1 ? 'trip' : 'trips'} · ${fmt(distance, 1)} km`
  return [[h('Div', { className: classes.join(' ') }, days.map((day, index) => dayGroup(day.trips, index)))], count]
}

function csvValue (value) {
  if (value === null || value === undefined) return ''
  const text = String(value)
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

function psaccExportTrips (clicks, data, range) { // eslint-disable-line no-unused-vars
  if (!clicks) return noUpdate()
  const columns = ['id', 'start_at', 'end_at', 'duration', 'distance', 'speed_average', 'start_level', 'end_level',
    'consumption', 'consumption_km', 'consumption_fuel', 'consumption_fuel_km', 'mileage', 'altitude_diff',
    'consumption_by_temp', 'source', 'in_progress']
  const rows = tripsInRange(data, range).reverse().map(trip => columns.map(column => {
    if (column === 'start_at') return new Date(toMs(trip.start_at)).toISOString()
    if (column === 'end_at') return new Date(tripEnd(trip)).toISOString()
    const value = trip[column]
    // floats as the server computed them carry noise (7.600000000000364 km)
    return typeof value === 'number' && !Number.isInteger(value) ? Number(value.toFixed(3)) : value
  }).map(csvValue).join(','))
  return { content: [columns.join(','), ...rows].join('\n') + '\n', filename: 'trips.csv', type: 'text/csv' }
}

/* the route psacc recorded, or null when it has fewer than two distinct points */
function routeFigure (trip) {
  const positions = trip.positions || { lat: [], long: [] }
  const points = positions.lat.map((lat, i) => [lat, positions.long[i]])
    .filter(([lat, long]) => isNumber(lat) && isNumber(long))
  if (new Set(points.map(p => p.join(','))).size < 2) return null
  const lats = points.map(p => p[0])
  const longs = points.map(p => p[1])
  const figure = {
    data: [{ type: 'scattermap', mode: 'lines', lat: lats, lon: longs, line: { width: 4 }, hoverinfo: 'skip' }],
    layout: {
      map: {
        style: 'style.json',
        center: { lat: (Math.min(...lats) + Math.max(...lats)) / 2, lon: (Math.min(...longs) + Math.max(...longs)) / 2 },
        zoom: 12
      },
      margin: { t: 0, b: 0, l: 0, r: 0 },
      showlegend: false
    }
  }
  return window.psaccTheme ? window.psaccTheme.themeFigure(figure) : figure
}

/* the details' title, route figure, and whether the route or its absence shows */
function psaccTripDetails (clicks, data) { // eslint-disable-line no-unused-vars
  const triggered = window.dash_clientside.callback_context.triggered
  if (!triggered.length || !triggered[0].value) return Array(4).fill(noUpdate())
  const index = JSON.parse(triggered[0].prop_id.split('.n_clicks')[0]).index
  const trip = ((data && data.trips) || []).find(t => t.id === index)
  if (!trip) return ['Trip', noUpdate(), { display: 'none' }, {}]
  const start = toMs(trip.start_at)
  const title = `${dayLabel(start)}, ${hhmm(start)} – ${hhmm(tripEnd(trip))} · ${fmt(trip.distance || 0, 1)} km`
  const route = routeFigure(trip)
  if (!route) return [title, noUpdate(), { display: 'none' }, {}]
  return [title, route, {}, { display: 'none' }]
}
