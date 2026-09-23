/* The summary's period picker and its "last day driven" figures, computed from the page's stores
   (see web/view/dashboard.py for the callbacks calling these). */

const PERIOD_KEYS = ['7', '30', '365', 'all', 'custom']
const DAY_MS = 24 * 3600 * 1000

function noUpdate () {
  return window.dash_clientside.no_update
}

/* the period of the button just clicked */
function psaccPeriodClicked () { // eslint-disable-line no-unused-vars
  const triggered = window.dash_clientside.callback_context.triggered
  if (!triggered.length || !triggered[0].value) return noUpdate()
  return triggered[0].prop_id.split('.')[0].replace('period-', '')
}

/* the slider's range for a period (seconds, like the slider), whether the slider shows, the buttons' classes */
function psaccApplyPeriod (period, min, max, value) { // eslint-disable-line no-unused-vars
  const key = PERIOD_KEYS.includes(period) ? period : 'all'
  const classes = PERIOD_KEYS.map(k => 'psacc-seg' + (k === key ? ' active' : ''))
  let range = noUpdate()
  if (key !== 'custom' && min != null && max != null) {
    let start = min
    if (key !== 'all') {
      start = Math.min(Math.max(min, Date.now() / 1000 - Number(key) * DAY_MS / 1000), max)
    }
    range = [start, max]
  }
  return [range, key === 'custom', ...classes]
}

function psaccPeriodLabel (value) { // eslint-disable-line no-unused-vars
  if (!value) return ''
  const options = { day: 'numeric', month: 'short', year: 'numeric' }
  const [start, end] = value.map(seconds => new Date(seconds * 1000).toLocaleDateString(undefined, options))
  return start === end ? start : `${start} – ${end}`
}

function toMs (date) {
  return typeof date === 'number' ? date : new Date(date).getTime()
}

function isNumber (value) {
  return typeof value === 'number' && !Number.isNaN(value)
}

function dayKey (ms) {
  const date = new Date(ms)
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`
}

function pad (n) {
  return String(n).padStart(2, '0')
}

/* local time as plotly reads it (it doesn't convert time zones) */
function plotTime (ms) {
  const d = new Date(ms)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function hhmm (ms) {
  const d = new Date(ms)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function dayLabel (ms) {
  return new Date(ms).toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })
}

function fmt (value, digits) {
  return new Intl.NumberFormat(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value)
}

function cssColor (name) {
  return window.getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

/* a dash html component, as a callback returns it */
function h (type, props, children) {
  const allProps = Object.assign({}, props)
  if (children !== undefined) allProps.children = children
  return { type, namespace: 'dash_html_components', props: allProps }
}

function tripEnd (trip) {
  if (trip.end_at) return toMs(trip.end_at)
  return toMs(trip.start_at) + (trip.duration || 0) * 60000
}

function batteryFigure (dayTrips, dayCharges) {
  const accent = cssColor('--psacc-accent')
  const drive = cssColor('--psacc-drive')
  const points = []
  dayTrips.forEach(trip => {
    if (isNumber(trip.start_level)) points.push([toMs(trip.start_at), trip.start_level])
    if (isNumber(trip.end_level)) points.push([tripEnd(trip), trip.end_level])
  })
  dayCharges.forEach(charge => {
    if (isNumber(charge.start_level)) points.push([toMs(charge.start_at), charge.start_level])
    if (isNumber(charge.end_level) && charge.stop_at) points.push([toMs(charge.stop_at), charge.end_level])
  })
  points.sort((a, b) => a[0] - b[0])
  const times = points.map(p => p[0])
    .concat(dayTrips.map(t => toMs(t.start_at)), dayTrips.map(tripEnd), dayCharges.map(c => toMs(c.start_at)))
  const layout = {
    margin: { l: 44, r: 12, t: 8, b: 28 },
    showlegend: false,
    hovermode: 'closest',
    xaxis: { type: 'date', tickformat: '%H:%M', fixedrange: true, showgrid: false },
    yaxis: { range: [0, 105], ticksuffix: '%', dtick: 25, fixedrange: true },
    shapes: [],
    annotations: []
  }
  if (times.length) {
    const margin = 30 * 60000
    layout.xaxis.range = [plotTime(Math.min(...times) - margin), plotTime(Math.max(...times) + margin)]
  }
  dayCharges.forEach(charge => {
    if (!charge.stop_at) return
    const start = toMs(charge.start_at)
    const stop = toMs(charge.stop_at)
    layout.shapes.push({
      type: 'rect',
      xref: 'x',
      yref: 'paper',
      x0: plotTime(start),
      x1: plotTime(stop),
      y0: 0,
      y1: 1,
      fillcolor: accent,
      opacity: 0.12,
      line: { width: 0 },
      layer: 'below'
    })
    if (isNumber(charge.start_level) && isNumber(charge.end_level)) {
      layout.annotations.push({
        x: plotTime((start + stop) / 2),
        y: 0.5,
        xref: 'x',
        yref: 'paper',
        showarrow: false,
        text: `Charged ${fmt(charge.start_level, 0)}% → ${fmt(charge.end_level, 0)}%`
      })
    }
  })
  dayTrips.forEach(trip => {
    layout.shapes.push({
      type: 'rect',
      xref: 'x',
      yref: 'paper',
      x0: plotTime(toMs(trip.start_at)),
      x1: plotTime(tripEnd(trip)),
      y0: 0,
      y1: 0.035,
      fillcolor: drive,
      line: { width: 0 }
    })
  })
  if (!points.length) {
    layout.annotations.push({ x: 0.5, y: 0.55, xref: 'paper', yref: 'paper', showarrow: false, text: 'No battery reading on this day' })
  }
  const figure = {
    data: [{
      type: 'scatter',
      mode: 'lines+markers',
      x: points.map(p => plotTime(p[0])),
      y: points.map(p => p[1]),
      line: { width: 2.5 },
      marker: { size: 6 },
      hovertemplate: '%{y:.0f}%<extra></extra>',
      name: 'Battery'
    }],
    layout
  }
  return window.psaccTheme ? window.psaccTheme.themeFigure(figure) : figure
}

function tripRows (dayTrips) {
  const longest = Math.max(...dayTrips.map(t => t.distance || 0), 0.1)
  return dayTrips.map(trip => h('Div', { className: 'psacc-day-trip' }, [
    h('Span', { className: 'num psacc-day-trip-time' }, hhmm(toMs(trip.start_at))),
    h('Div', { className: 'psacc-bar' },
      h('Div', { className: 'psacc-bar-fill', style: { width: `${Math.max(1, 100 * (trip.distance || 0) / longest)}%` } })),
    h('Span', { className: 'num psacc-day-trip-km' },
      trip.in_progress
        ? [h('Span', { className: 'psacc-chip' }, 'driving'), ` ${fmt(trip.distance || 0, 1)} km`]
        : `${fmt(trip.distance || 0, 1)} km`)
  ]))
}

function figureCard (label, value, sub) {
  return [h('Div', { className: 'psacc-card-label' }, label),
    h('Div', { className: 'psacc-card-value num' }, value),
    h('Div', { className: 'psacc-card-sub' }, sub)]
}

function lastChargeCard (charges, currency) {
  if (!charges.length) return figureCard('Last charge', '–', 'No charge in this period')
  const charge = charges[charges.length - 1]
  const parts = []
  if (isNumber(charge.start_level) && isNumber(charge.end_level)) {
    parts.push(`${fmt(charge.start_level, 0)}% → ${fmt(charge.end_level, 0)}%`)
  }
  if (isNumber(charge.kw)) parts.push(`${fmt(charge.kw, 2)} kWh`)
  if (isNumber(charge.price)) parts.push(`${fmt(charge.price, 2)} ${currency}`)
  const start = toMs(charge.start_at)
  let when = `${dayLabel(start)}, ${hhmm(start)}`
  if (charge.stop_at) when += `–${hhmm(toMs(charge.stop_at))}`
  if (charge.duration_str) when += ` (${charge.duration_str})`
  return figureCard('Last charge', parts.join(' · ') || 'Charged', when)
}

function odometerCard (trips) {
  const located = trips.filter(t => isNumber(t.mileage))
  if (!located.length) return figureCard('Odometer', '–', 'No trip in this period')
  const last = located[located.length - 1]
  return figureCard('Odometer', `${fmt(last.mileage, 1)} km`,
    `after the ${hhmm(toMs(last.start_at))} trip, ${dayLabel(toMs(last.start_at))}`)
}

/* the fuel card, and its style: hidden for a car without a tank */
function fuelCard (trips, dayTrips, day) {
  const withLevel = trips.filter(t => isNumber(t.end_level_fuel))
  if (!withLevel.length) return [[], { display: 'none' }]
  const level = withLevel[withLevel.length - 1].end_level_fuel
  const litres = dayTrips.reduce((sum, t) => sum + (isNumber(t.consumption_fuel) ? t.consumption_fuel : 0), 0)
  return [figureCard('Fuel', `${fmt(level, 0)}% in the tank`, `${fmt(litres, 2)} L used on ${day}`), {}]
}

function psaccSummaryDay (data, range, config) { // eslint-disable-line no-unused-vars
  if (!data || !range) return Array(9).fill(noUpdate())
  const currency = (config && config.currency) || '€'
  const inRange = date => { const s = toMs(date) / 1000; return s >= range[0] && s <= range[1] }
  const byStart = (a, b) => toMs(a.start_at) - toMs(b.start_at)
  const trips = (data.trips || []).filter(t => inRange(t.start_at)).sort(byStart)
  const charges = (data.chargings || []).filter(c => inRange(c.start_at)).sort(byStart)
  const latest = Math.max(...trips.map(t => toMs(t.start_at)), ...charges.map(c => toMs(c.start_at)), -Infinity)
  const [fuel, fuelStyle] = fuelCard(trips, [], '')
  if (latest === -Infinity) {
    const empty = batteryFigure([], [])
    return [empty, 'Battery', 'Trips', 'Nothing recorded in this period', [], lastChargeCard(charges, currency),
      odometerCard(trips), fuel, fuelStyle]
  }
  const key = dayKey(latest)
  const day = dayLabel(latest)
  const dayTrips = trips.filter(t => dayKey(toMs(t.start_at)) === key)
  const dayCharges = charges.filter(c => dayKey(toMs(c.start_at)) === key ||
    (c.stop_at && dayKey(toMs(c.stop_at)) === key))
  const distance = dayTrips.reduce((sum, t) => sum + (t.distance || 0), 0)
  const litres = dayTrips.reduce((sum, t) => sum + (isNumber(t.consumption_fuel) ? t.consumption_fuel : 0), 0)
  let sub = `${dayTrips.length} ${dayTrips.length === 1 ? 'trip' : 'trips'} · ${fmt(distance, 1)} km`
  if (litres > 0) sub += ` · ${fmt(litres, 2)} L fuel`
  const [dayFuel, dayFuelStyle] = fuelCard(trips, dayTrips, day)
  return [
    batteryFigure(dayTrips, dayCharges),
    `Battery · ${day}`,
    `Trips · ${day}`,
    dayTrips.length ? sub : 'No trip on this day',
    tripRows(dayTrips),
    lastChargeCard(charges, currency),
    odometerCard(trips),
    dayFuel,
    dayFuelStyle
  ]
}
