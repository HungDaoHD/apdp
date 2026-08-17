// Runs once, only when the mongo container's data volume is empty (the
// official image's own convention for /docker-entrypoint-initdb.d scripts).
// Creates a database-scoped app user with readWrite only — the app never
// authenticates as the cluster's root/admin account, so a compromised app
// process can't touch any other database or run admin commands.
//
// MONGO_APP_USER / MONGO_APP_PASSWORD / MONGO_INITDB_DATABASE come from the
// mongo service's own `environment:` in docker-compose.yml; the official
// image exposes container env vars to init scripts via `process.env`.
const dbName = process.env.MONGO_INITDB_DATABASE || 'ap_workload';

db = db.getSiblingDB(dbName);
db.createUser({
  user: process.env.MONGO_APP_USER,
  pwd: process.env.MONGO_APP_PASSWORD,
  roles: [{role: 'readWrite', db: dbName}],
});
