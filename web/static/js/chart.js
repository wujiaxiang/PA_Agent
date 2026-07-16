// chart.js — K-line chart via Lightweight Charts

function createChart(container) {
  const chart = LightweightCharts.createChart(container, {
    layout: {
      background: { type: 'solid', color: '#131722' },
      textColor: '#d1d4dc',
    },
    grid: {
      vertLines: { color: '#1e222d' },
      horzLines: { color: '#1e222d' },
    },
    crosshair: { mode: 0 },
    rightPriceScale: { borderColor: '#363c4e' },
    timeScale: { borderColor: '#363c4e', timeVisible: true },
  });

  const candleSeries = chart.addCandlestickSeries({
    upColor: '#26a69a',
    downColor: '#ef5350',
    borderUpColor: '#26a69a',
    borderDownColor: '#ef5350',
    wickUpColor: '#26a69a',
    wickDownColor: '#ef5350',
  });

  return { chart, candleSeries };
}

function setBars(candleSeries, bars) {
  const data = bars.map(b => ({
    time: b.ts_open / 1000, // LW Charts uses seconds
    open: b.open,
    high: b.high,
    low: b.low,
    close: b.close,
  }));
  // Sort ascending by time for the chart library
  data.sort((a, b) => a.time - b.time);
  candleSeries.setData(data);
}
