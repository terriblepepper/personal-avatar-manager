"""Install the PostgreSQL domains and upgrade existing data in one locked transaction."""
from avatar.core.postgres_helpers import STATEMENTS as HELPERS
from avatar.core.postgres_schema_runs import STATEMENTS as RUNS
from avatar.core.schema import accounts, assets, chat, collaboration, protocol
from avatar.core.schema.performance import install_business_keys, install_indexes
from avatar.core.schema.upgrades import upgrade


def install_core_schema(connection):
    install_business_keys(connection)
    for domain in (accounts, assets, chat, protocol, collaboration):
        for statement in domain.STATEMENTS:
            connection.execute(statement)
    for statement in (*HELPERS, *RUNS):
        connection.execute(statement)
    upgrade(connection)
    install_indexes(connection)
