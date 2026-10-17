"""Add admin username to connections, for services that sign in as an admin

Revision ID: 20261017_connection_admin_username
Revises: 20261013_plex_invite_restrictions
Create Date: 2026-10-17 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20261017_connection_admin_username"
down_revision = "20261013_plex_invite_restrictions"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("ombi_connection", schema=None) as batch_op:
        batch_op.add_column(sa.Column("admin_username", sa.String(), nullable=True))


def downgrade():
    with op.batch_alter_table("ombi_connection", schema=None) as batch_op:
        batch_op.drop_column("admin_username")
