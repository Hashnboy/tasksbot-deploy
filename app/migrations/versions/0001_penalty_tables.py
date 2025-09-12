"""add penalty tables"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0001_penalties'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'penalty_policies',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('is_active', sa.Boolean, server_default=sa.text('true')),
        sa.Column('scope', JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column('strictness', sa.Enum('lenient','standard','strict','custom', name='policy_strictness'), server_default='standard'),
        sa.Column('rules', JSONB, server_default=sa.text("'[]'::jsonb")),
        sa.Column('caps', JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column('grace', JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column('forgiveness', JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column('escalation', JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime, server_default=sa.func.now()),
    )
    op.create_table(
        'penalty_events',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('occurred_at', sa.DateTime, server_default=sa.func.now(), index=True),
        sa.Column('user_id', sa.Integer, nullable=False, index=True),
        sa.Column('direction_id', sa.Integer, index=True),
        sa.Column('point_id', sa.Integer, index=True),
        sa.Column('source', sa.Enum('late','missed_checkin','geofence_fail','media_blur','media_duplicate','face_mismatch','task_reject','task_overdue','verify_sla_breach','receiving_delay','receiving_mismatch','procurement_dup','anomaly_sales_stock', name='penalty_source'), nullable=False),
        sa.Column('payload', JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column('dedupe_key', sa.String(255), index=True),
        sa.Column('severity', sa.Enum('low','medium','high','critical', name='penalty_severity'), server_default='low'),
    )
    op.create_index('idx_penalty_events_user_time','penalty_events',['user_id','occurred_at'])
    op.create_table(
        'penalty_ledger',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('event_id', sa.Integer, sa.ForeignKey('penalty_events.id'), nullable=False),
        sa.Column('user_id', sa.Integer, nullable=False),
        sa.Column('policy_id', sa.Integer, sa.ForeignKey('penalty_policies.id'), nullable=False),
        sa.Column('applied_at', sa.DateTime, server_default=sa.func.now(), index=True),
        sa.Column('points', sa.Integer, server_default='0'),
        sa.Column('amount', sa.Numeric(10,2)),
        sa.Column('reasons', JSONB, server_default=sa.text("'[]'::jsonb")),
        sa.Column('status', sa.Enum('applied','waived','reversed', name='ledger_status'), server_default='applied'),
        sa.Column('waiver_reason', sa.Text),
        sa.Column('reversed_by_user_id', sa.Integer),
    )
    op.create_index('idx_penalty_ledger_user_applied','penalty_ledger',['user_id','applied_at'])
    op.create_table(
        'appeals',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('ledger_id', sa.Integer, sa.ForeignKey('penalty_ledger.id'), nullable=False),
        sa.Column('user_id', sa.Integer, nullable=False),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now()),
        sa.Column('status', sa.Enum('open','approved','rejected', name='appeal_status'), server_default='open'),
        sa.Column('moderator_user_id', sa.Integer),
        sa.Column('decision_comment', sa.Text),
        sa.Column('decided_at', sa.DateTime),
    )
    op.create_table(
        'probations',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('user_id', sa.Integer, nullable=False),
        sa.Column('started_at', sa.DateTime, server_default=sa.func.now()),
        sa.Column('ends_at', sa.DateTime, nullable=False),
        sa.Column('reason', sa.Text),
        sa.Column('policy_snapshot', JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column('is_active', sa.Boolean, server_default=sa.text('true')),
    )
    op.create_table(
        'rewards',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('user_id', sa.Integer, nullable=False),
        sa.Column('points', sa.Integer, server_default='0'),
        sa.Column('badge', sa.String(50), nullable=False),
        sa.Column('granted_at', sa.DateTime, server_default=sa.func.now()),
        sa.Column('comment', sa.Text),
    )
    op.create_table(
        'kpi_snapshots',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('period_start', sa.DateTime, nullable=False),
        sa.Column('period_end', sa.DateTime, nullable=False),
        sa.Column('direction_id', sa.Integer),
        sa.Column('point_id', sa.Integer),
        sa.Column('user_id', sa.Integer),
        sa.Column('data', JSONB, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index('uq_kpi_period','kpi_snapshots',['period_start','period_end','direction_id','point_id','user_id'],unique=True)


def downgrade():
    op.drop_index('uq_kpi_period', table_name='kpi_snapshots')
    op.drop_table('kpi_snapshots')
    op.drop_table('rewards')
    op.drop_table('probations')
    op.drop_table('appeals')
    op.drop_index('idx_penalty_ledger_user_applied', table_name='penalty_ledger')
    op.drop_table('penalty_ledger')
    op.drop_index('idx_penalty_events_user_time', table_name='penalty_events')
    op.drop_table('penalty_events')
    op.drop_table('penalty_policies')
