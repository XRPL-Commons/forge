import assert from 'node:assert/strict'
import http from 'node:http'

function post(base, chunks) {
  return new Promise((resolve, reject) => {
    const target = new URL('/api/settings/sync', base)
    const request = http.request(target, { method: 'POST', headers: {
      origin: base, 'content-type': 'application/json', 'x-forge-action': 'synthetic',
    } }, response => {
      response.resume()
      response.once('end', () => resolve(response.statusCode))
    })
    request.on('error', reject)
    for (const chunk of chunks) request.write(chunk)
    request.end()
  })
}

export default async function (page, { base, scenario, requests }) {
  await scenario('default')
  assert.equal(await post(base, ['{', '}']), 200, 'bounded chunked JSON reaches the API')
  assert.equal(await post(base, []), 400, 'empty POST is a bad request, not oversized')
  assert.equal(await post(base, ['x'.repeat(262145)]), 413)
  const writes = (await requests()).filter(request => request.method === 'POST' && request.path === '/api/settings/sync')
  assert.equal(writes.length, 1, 'invalid bodies never reach the API')
  assert.deepEqual(writes[0].body, {})
  // A client disconnect during upload cannot kill the proxy or dispatch a write.
  await new Promise(resolve => {
    const request = http.request(new URL('/api/settings/sync', base), { method: 'POST', headers: {
      origin: base, 'content-type': 'application/json', 'x-forge-action': 'synthetic', 'content-length': '20',
    } })
    request.on('error', () => resolve())
    request.write('{')
    request.once('socket', socket => socket.once('connect', () => { request.destroy(); resolve() }))
  })
  assert.equal((await fetch(base + '/api/factory')).status, 200)
}
