# The Worker run locally by workerd, for the tests: the bundle (make worker-build), its Durable
# Objects' SQLite storage on disk, test hooks on, and outbound fetch allowed to loopback (anvil,
# the mock builder). Serve with:
#
#   workerd serve worker/workerd.capnp --socket-addr http=127.0.0.1:8547 \
#     --directory-path do-storage=dist/workerd-storage

using Workerd = import "/workerd/workerd.capnp";

const config :Workerd.Config = (
  services = [
    (name = "relay", worker = .relay),
    (
      name = "internet",
      network = (allow = ["public", "private", "local"], tlsOptions = (trustBrowserCas = true)),
    ),
    (name = "do-storage", disk = (path = "/nonexistent", writable = true)),
  ],
  sockets = [(name = "http", address = "127.0.0.1:8547", http = (), service = "relay")],
);

const relay :Workerd.Worker = (
  modules = [(name = "worker.js", esModule = embed "../dist/worker/worker.js")],
  compatibilityDate = "2026-10-01",
  bindings = [
    (name = "RELAY", durableObjectNamespace = "ChainRelay"),
    (name = "TEST_HOOKS", text = "1"),
    # the chains the root lists, with the payee the tests configure (anvil's account 8); tests
    # configure each chain through the hooks
    (
      name = "CONFIG",
      text = "{\"chains\": {\"31337\": {\"payee\": \"0x23618e81E3f5cdF7f54C3d65f7FBc0aBf5B21E8f\"}}}",
    ),
    (name = "FLOAT_KEY", text = ""),
  ],
  durableObjectNamespaces = [
    (className = "ChainRelay", uniqueKey = "bruno-chain-relay", enableSql = true),
  ],
  durableObjectStorage = (localDisk = "do-storage"),
  globalOutbound = "internet",
);
