// Offline round trips, run inside the same native sandbox as repository tests.
const mongoose = require('/usr/local/lib/node_modules/mongoose')
const { execFileSync } = require('node:child_process')

async function main() {
  try {
    await mongoose.connect(process.env.MONGODB_URI, { serverSelectionTimeoutMS: 5000 })
    const Record = mongoose.model('Canary', new mongoose.Schema({ value: String }))
    const row = await Record.create({ value: 'forge-services-ok' })
    const read = await Record.findById(row._id).lean()
    if (read.value !== 'forge-services-ok') throw new Error('MongoDB round trip failed')
    await Record.deleteMany({})
    const sql = 'CREATE TEMP TABLE canary (v text); INSERT INTO canary VALUES (\'forge-services-ok\'); SELECT v FROM canary;'
    const pg = execFileSync('psql', [process.env.DATABASE_URL, '-v', 'ON_ERROR_STOP=1', '-Atc', sql], { timeout: 5000, encoding: 'utf8' })
    if (!pg.includes('forge-services-ok')) throw new Error('PostgreSQL round trip failed')
    execFileSync('redis-cli', ['-u', process.env.REDIS_URL, 'SET', 'canary', 'forge-services-ok'], { timeout: 5000 })
    const redis = execFileSync('redis-cli', ['-u', process.env.REDIS_URL, 'GET', 'canary'], { timeout: 5000, encoding: 'utf8' })
    if (redis.trim() !== 'forge-services-ok') throw new Error('Redis round trip failed')
    console.log('forge-services-ok')
  } finally {
    await mongoose.disconnect()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
