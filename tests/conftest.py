import os

# Unit tests must not depend on the live register: unknown lookups keep every competing name (fail closed).
os.environ.setdefault("SIGNALPOST_REGISTRY_NAME_CHECK", "0")
