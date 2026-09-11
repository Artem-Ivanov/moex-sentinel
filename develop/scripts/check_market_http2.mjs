import http2 from 'node:http2';
import fs from 'node:fs';

const target = process.env.MARKET_PROBE_TARGET;
if (target !== 'sandbox-invest-public-api.tbank.ru:443') process.exit(2);
const metadata = JSON.parse(process.env.MARKET_PROBE_METADATA);
const payload = Buffer.from(process.env.MARKET_PROBE_REQUEST, 'base64');
function frame(value) {
  const framed = Buffer.alloc(5 + value.length);
  framed.writeUInt32BE(value.length, 1);
  value.copy(framed, 5);
  return framed;
}
const path = '/tinkoff.public.invest.api.contract.v1.MarketDataStreamService/MarketDataStream';

async function probe(keepOpen, label, rpcPath = path, body = payload) {
  return new Promise((resolve) => {
    const started = performance.now();
    const observed = { mode: label || (keepOpen ? 'open' : 'half_closed'), data_chunks: 0 };
    const client = http2.connect(`https://${target}`, { ca: fs.readFileSync(process.env.MARKET_PROBE_CA) });
    let request;
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      observed.latency_ms = Math.round(performance.now() - started);
      if (request) request.close();
      client.destroy();
      resolve(observed);
    };
    const timer = setTimeout(() => { observed.timeout = true; finish(); }, 15000);
    const headers = (values) => {
      if (values[':status']) observed.http_status = Number(values[':status']);
      if (values['grpc-status']) observed.grpc_status = Number(values['grpc-status']);
      const message = String(values['grpc-message'] || '').toLowerCase();
      if (message.includes('method not found')) observed.method_not_found = true;
    };
    client.on('error', () => { observed.session_error = true; finish(); });
    client.on('connect', () => {
      request = client.request({ ':method': 'POST', ':path': rpcPath, 'content-type': 'application/grpc', te: 'trailers', ...metadata });
      request.on('response', headers);
      request.on('trailers', headers);
      request.on('data', () => { observed.data_chunks += 1; });
      request.on('error', () => { observed.stream_error = true; });
      request.on('close', () => { observed.rst_code = request.rstCode; finish(); });
      if (keepOpen) request.write(frame(body));
      else request.end(frame(body));
    });
  });
}

const checks = [probe(true), probe(false)];
if (process.env.MARKET_PROBE_CONTROLS === '1') {
  checks.push(probe(false, 'unary_book', '/tinkoff.public.invest.api.contract.v1.MarketDataService/GetOrderBook', Buffer.from(process.env.MARKET_PROBE_BOOK_REQUEST, 'base64')));
  checks.push(probe(false, 'nonexistent_method', '/tinkoff.public.invest.api.contract.v1.MarketDataStreamService/DiagnosticNonexistentMethod', Buffer.alloc(0)));
}
console.log(JSON.stringify(await Promise.all(checks)));
