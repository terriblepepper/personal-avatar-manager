"""Install the application stores inside the database startup transaction."""
from avatar.core.postgres_schema import install_core_schema
from avatar.knowledge.everos_storage import ensure_schema
from avatar.protocols.tasks.store import initialize_system_tasks
from avatar.rag.stores.postgres import install_vector_schema


def install_schema(connection):
    install_core_schema(connection)
    initialize_system_tasks(connection)
    install_vector_schema(connection)
    ensure_schema(connection)
