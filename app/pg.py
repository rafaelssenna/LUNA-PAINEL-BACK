import os
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row  # <- cada fetch* já vem como dict

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """
    Singleton do pool de conexões (autocommit ligado, row_factory=dict_row).
    """
    global _pool
    if _pool is None:
        dsn = os.getenv("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL não definido no ambiente em runtime")

        size = int(os.getenv("PGPOOL_SIZE", "5"))

        def _configure(conn):
            conn.autocommit = True

        _pool = ConnectionPool(
            conninfo=dsn,
            max_size=size,
            configure=_configure,
            kwargs={"row_factory": dict_row},
        )
    return _pool


# helper opcional (uso: with get_conn() as con: con.execute(...))
def get_conn():
    return get_pool().connection()


def init_schema():
    """
    - Tabela instances -> instâncias WhatsApp do usuário
    - Tabela lead_status -> garante migrações/índices
    - Tabela billing_accounts -> trial + cobrança
    - Tabela users -> login por e-mail/senha
    - Tabela messages -> armazenamento local de mensagens
    """
    sql = """
    -- =========================================
    -- INSTANCES (WhatsApp)
    -- =========================================
    CREATE TABLE IF NOT EXISTS instances (
      id              TEXT PRIMARY KEY,
      user_id         INTEGER NOT NULL,
      instance_id     TEXT,  -- Duplicação do id para compatibilidade
      uazapi_token    TEXT NOT NULL,
      uazapi_host     TEXT NOT NULL,
      status          TEXT NOT NULL DEFAULT 'disconnected',
      admin_status    TEXT NOT NULL DEFAULT 'pending_config',
      phone_number    TEXT,
      phone_name      TEXT,
      prompt          TEXT,
      admin_notes     TEXT,
      redirect_phone  TEXT,  -- Número para handoff
      configured_by   INTEGER,  -- ID do admin que configurou
      configured_at   TIMESTAMPTZ,
      prompt_history  JSONB DEFAULT '[]'::jsonb,
      created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at      TIMESTAMPTZ
    );
    
    -- Migração: converter id de UUID para TEXT (se necessário)
    DO $$
    BEGIN
      -- Se a coluna id existir como UUID, recriar tabela
      IF EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'instances' 
        AND column_name = 'id' 
        AND data_type = 'uuid'
      ) THEN
        -- Backup dos dados existentes
        CREATE TEMP TABLE instances_backup AS SELECT * FROM instances;
        DROP TABLE instances CASCADE;
        
        -- Recriar tabela com id TEXT
        CREATE TABLE instances (
          id              TEXT PRIMARY KEY,
          user_id         INTEGER NOT NULL,
          uazapi_token    TEXT NOT NULL,
          uazapi_host     TEXT NOT NULL,
          status          TEXT NOT NULL DEFAULT 'disconnected',
          admin_status    TEXT NOT NULL DEFAULT 'pending_config',
          phone_number    TEXT,
          created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at      TIMESTAMPTZ
        );
        
        -- Restaurar dados (convertendo UUID para TEXT)
        INSERT INTO instances 
        SELECT id::TEXT, user_id, uazapi_token, uazapi_host, status, admin_status, 
               phone_number, created_at, updated_at 
        FROM instances_backup;
        
        DROP TABLE instances_backup;
      END IF;
    END$$;
    
    CREATE INDEX IF NOT EXISTS idx_instances_user_id ON instances(user_id);
    CREATE INDEX IF NOT EXISTS idx_instances_status ON instances(status);
    
    -- =========================================
    -- LEAD STATUS
    -- =========================================
    CREATE TABLE IF NOT EXISTS lead_status (
      instance_id   TEXT NOT NULL,
      chat_id       TEXT NOT NULL,
      stage         TEXT NOT NULL DEFAULT 'contatos',
      updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      last_msg_ts   BIGINT NOT NULL DEFAULT 0,
      last_from_me  BOOLEAN NOT NULL DEFAULT FALSE,
      PRIMARY KEY (instance_id, chat_id)
    );

    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'lead_status' AND column_name = 'instance_id'
      ) THEN
        ALTER TABLE lead_status ADD COLUMN instance_id TEXT;
      END IF;
    END$$;

    UPDATE lead_status
       SET instance_id = COALESCE(instance_id, 'legacy')
     WHERE instance_id IS NULL;

    ALTER TABLE lead_status
      ALTER COLUMN stage SET DEFAULT 'contatos',
      ALTER COLUMN updated_at SET DEFAULT NOW(),
      ALTER COLUMN last_msg_ts SET DEFAULT 0,
      ALTER COLUMN last_from_me SET DEFAULT FALSE;

    DO $$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'lead_status'::regclass AND contype = 'p'
      ) THEN
        ALTER TABLE lead_status DROP CONSTRAINT IF EXISTS lead_status_pkey;
      END IF;
      BEGIN
        ALTER TABLE lead_status ALTER COLUMN instance_id SET NOT NULL;
      EXCEPTION WHEN others THEN
        NULL;
      END;
      BEGIN
        ALTER TABLE lead_status ADD PRIMARY KEY (instance_id, chat_id);
      EXCEPTION WHEN others THEN
        NULL;
      END;
    END$$;

    CREATE INDEX IF NOT EXISTS idx_lead_status_stage          ON lead_status(stage);
    CREATE INDEX IF NOT EXISTS idx_lead_status_updated_at     ON lead_status(updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_lead_status_last_msg_ts    ON lead_status(last_msg_ts DESC);
    CREATE INDEX IF NOT EXISTS idx_lead_status_inst_stage     ON lead_status(instance_id, stage);
    CREATE INDEX IF NOT EXISTS idx_lead_status_inst_last_ts   ON lead_status(instance_id, last_msg_ts DESC);

    -- =========================================
    -- BILLING / ASSINATURAS
    -- =========================================
    CREATE TABLE IF NOT EXISTS billing_accounts (
      id                  SERIAL PRIMARY KEY,
      billing_key         TEXT UNIQUE NOT NULL,
      instance_id         TEXT,
      host                TEXT,
      created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      trial_started_at    TIMESTAMPTZ,
      trial_ends_at       TIMESTAMPTZ,
      paid_until          TIMESTAMPTZ,
      plan                TEXT,
      last_payment_status TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_billing_paid_until ON billing_accounts(paid_until DESC);
    CREATE INDEX IF NOT EXISTS idx_billing_trial_ends ON billing_accounts(trial_ends_at DESC);

    -- =========================================
    -- USERS (login por e-mail/senha)
    -- =========================================
    CREATE TABLE IF NOT EXISTS users (
      id              SERIAL PRIMARY KEY,
      email           TEXT NOT NULL UNIQUE,
      password_hash   TEXT NOT NULL,
      created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      last_login_at   TIMESTAMPTZ NULL
    );

    -- =========================================
    -- MESSAGES (armazenamento local)
    -- =========================================
    CREATE TABLE IF NOT EXISTS messages (
      instance_id   TEXT        NOT NULL,
      chat_id       TEXT        NOT NULL,
      msgid         TEXT        NOT NULL,
      from_me       BOOLEAN     NOT NULL DEFAULT FALSE,
      timestamp     BIGINT      NOT NULL,
      content       TEXT,
      media_url     TEXT,
      media_mime    TEXT,
      created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      PRIMARY KEY (instance_id, chat_id, msgid)
    );

    CREATE INDEX IF NOT EXISTS idx_messages_chat_ts
      ON messages(instance_id, chat_id, timestamp DESC);

    CREATE INDEX IF NOT EXISTS idx_messages_ts
      ON messages(timestamp DESC);
    
    -- =========================================
    -- ADMIN USERS (administradores do sistema)
    -- =========================================
    CREATE TABLE IF NOT EXISTS admin_users (
      id              SERIAL PRIMARY KEY,
      email           TEXT NOT NULL UNIQUE,
      password_hash   TEXT NOT NULL,
      full_name       TEXT NOT NULL,
      role            TEXT NOT NULL DEFAULT 'admin',
      is_active       BOOLEAN NOT NULL DEFAULT TRUE,
      created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      last_login_at   TIMESTAMPTZ
    );
    
    -- Criar admin padrão (senha: admin123)
    INSERT INTO admin_users (email, password_hash, full_name, role)
    VALUES ('admin@luna.com', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5aeWZL8z.IjBe', 'Administrador', 'admin')
    ON CONFLICT (email) DO NOTHING;
    
    -- =========================================
    -- ADMIN ACTIONS (log de ações admin)
    -- =========================================
    CREATE TABLE IF NOT EXISTS admin_actions (
      id              SERIAL PRIMARY KEY,
      admin_id        INTEGER NOT NULL REFERENCES admin_users(id),
      action_type     TEXT NOT NULL,
      target_type     TEXT,
      target_id       TEXT,
      description     TEXT,
      created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    
    CREATE INDEX IF NOT EXISTS idx_admin_actions_created ON admin_actions(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_admin_actions_admin ON admin_actions(admin_id);
    
    -- =========================================
    -- NOTIFICATIONS (notificações para usuários)
    -- =========================================
    CREATE TABLE IF NOT EXISTS notifications (
      id              SERIAL PRIMARY KEY,
      recipient_type  TEXT NOT NULL,
      recipient_id    INTEGER NOT NULL,
      type            TEXT NOT NULL,
      title           TEXT NOT NULL,
      message         TEXT NOT NULL,
      read_at         TIMESTAMPTZ,
      created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    
    CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON notifications(recipient_type, recipient_id, created_at DESC);
    
    -- =========================================
    -- MIGRAÇÕES: Adicionar colunas novas à tabela instances
    -- =========================================
    DO $$
    BEGIN
      -- instance_id
      IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='instances' AND column_name='instance_id') THEN
        ALTER TABLE instances ADD COLUMN instance_id TEXT;
        UPDATE instances SET instance_id = id WHERE instance_id IS NULL;
      END IF;
      
      -- phone_name
      IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='instances' AND column_name='phone_name') THEN
        ALTER TABLE instances ADD COLUMN phone_name TEXT;
      END IF;
      
      -- prompt
      IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='instances' AND column_name='prompt') THEN
        ALTER TABLE instances ADD COLUMN prompt TEXT;
      END IF;
      
      -- admin_notes
      IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='instances' AND column_name='admin_notes') THEN
        ALTER TABLE instances ADD COLUMN admin_notes TEXT;
      END IF;
      
      -- redirect_phone
      IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='instances' AND column_name='redirect_phone') THEN
        ALTER TABLE instances ADD COLUMN redirect_phone TEXT;
      END IF;
      
      -- configured_by
      IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='instances' AND column_name='configured_by') THEN
        ALTER TABLE instances ADD COLUMN configured_by INTEGER;
      END IF;
      
      -- configured_at
      IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='instances' AND column_name='configured_at') THEN
        ALTER TABLE instances ADD COLUMN configured_at TIMESTAMPTZ;
      END IF;
      
      -- prompt_history
      IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='instances' AND column_name='prompt_history') THEN
        ALTER TABLE instances ADD COLUMN prompt_history JSONB DEFAULT '[]'::jsonb;
      END IF;
    END$$;

    -- =============================================================
    -- CAMPOS COMPATÍVEIS PARA TOKEN E HOST
    --
    -- Ao longo do desenvolvimento novos campos `token` e `host` foram
    -- utilizados pelo código para armazenar o token e o host da UAZAPI.
    -- Entretanto, versões anteriores da base de dados utilizavam
    -- exclusivamente `uazapi_token` e `uazapi_host`. Para garantir
    -- compatibilidade com ambas as versões e evitar erros de coluna
    -- inexistente, criamos as colunas `token` e `host` caso ainda
    -- não existam e preenchemos o seu valor a partir das colunas
    -- originais. Assim, queries que esperam `token` ou `host` não
    -- falharão, e as novas colunas refletem corretamente os dados
    -- existentes.
    DO $$
    BEGIN
      -- Adicionar coluna `token` se não existir
      IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='instances' AND column_name='token') THEN
        ALTER TABLE instances ADD COLUMN token TEXT;
        -- Copiar dados existentes de uazapi_token para token
        UPDATE instances SET token = uazapi_token WHERE token IS NULL;
      END IF;
      -- Adicionar coluna `host` se não existir
      IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='instances' AND column_name='host') THEN
        ALTER TABLE instances ADD COLUMN host TEXT;
        -- Copiar dados existentes de uazapi_host para host
        UPDATE instances SET host = uazapi_host WHERE host IS NULL;
      END IF;
    END$$;
    
    -- =========================================
    -- VIEWS E FUNCTIONS PARA PAINEL ADMIN
    -- =========================================
    
    -- View: Estatísticas do dashboard
    CREATE OR REPLACE VIEW v_admin_stats AS
    SELECT
      (SELECT COUNT(*) FROM instances) as total_instances,
      (SELECT COUNT(*) FROM instances WHERE admin_status = 'pending_config') as pending_config,
      (SELECT COUNT(*) FROM instances WHERE admin_status IN ('configured', 'active')) as active_instances,
      (SELECT COUNT(*) FROM instances WHERE status = 'connected') as connected_instances,
      (SELECT COUNT(*) FROM users) as total_users,
      (SELECT COUNT(*) FROM billing_accounts WHERE trial_ends_at > NOW() AND paid_until IS NULL) as users_on_trial,
      (SELECT COUNT(*) FROM billing_accounts WHERE paid_until > NOW()) as paying_users,
      (SELECT COUNT(*) FROM messages WHERE created_at::date = CURRENT_DATE) as messages_today;
    
    -- Function: Listar instâncias pendentes
    CREATE OR REPLACE FUNCTION get_pending_instances()
    RETURNS TABLE (
      instance_uuid TEXT,
      instance_id TEXT,
      user_email TEXT,
      user_name TEXT,
      phone_number TEXT,
      created_at TIMESTAMPTZ,
      hours_waiting NUMERIC
    ) AS $$
    BEGIN
      RETURN QUERY
      SELECT
        i.id as instance_uuid,
        i.instance_id,
        u.email as user_email,
        COALESCE(u.full_name, u.email) as user_name,
        i.phone_number,
        i.created_at,
        EXTRACT(EPOCH FROM (NOW() - i.created_at)) / 3600 as hours_waiting
      FROM instances i
      JOIN users u ON i.user_id = u.id
      WHERE i.admin_status = 'pending_config'
      ORDER BY i.created_at ASC;
    END;
    $$ LANGUAGE plpgsql;
    
    -- Adicionar campo full_name na tabela users se não existir
    DO $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='full_name') THEN
        ALTER TABLE users ADD COLUMN full_name TEXT;
      END IF;
    END$$;
    
    -- =========================================
    -- QUESTIONNAIRES (Questionários de onboarding)
    -- =========================================
    CREATE TABLE IF NOT EXISTS user_questionnaires (
      id                    SERIAL PRIMARY KEY,
      user_id               INTEGER NOT NULL REFERENCES users(id),
      has_whatsapp_number   BOOLEAN NOT NULL,
      company_name          TEXT NOT NULL,
      contact_phone         TEXT NOT NULL,
      contact_email         TEXT NOT NULL,
      product_service       TEXT NOT NULL,
      target_audience       TEXT NOT NULL,
      notification_phone    TEXT NOT NULL,
      prospecting_region    TEXT NOT NULL,
      created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      UNIQUE(user_id)
    );
    
    CREATE INDEX IF NOT EXISTS idx_questionnaires_user_id ON user_questionnaires(user_id);
    CREATE INDEX IF NOT EXISTS idx_questionnaires_created ON user_questionnaires(created_at DESC);
    
    -- =========================================
    -- AI MEMORY (Memória da IA por instância)
    -- =========================================
    CREATE TABLE IF NOT EXISTS ai_memory (
      id                SERIAL PRIMARY KEY,
      instance_id       TEXT NOT NULL,
      role              TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
      content           TEXT NOT NULL,
      timestamp         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      metadata          JSONB DEFAULT '{}'::jsonb
    );
    
    CREATE INDEX IF NOT EXISTS idx_ai_memory_instance ON ai_memory(instance_id);
    CREATE INDEX IF NOT EXISTS idx_ai_memory_timestamp ON ai_memory(instance_id, timestamp DESC);
    
    -- Comentário
    COMMENT ON TABLE ai_memory IS 'Memória de conversas da IA por instância para contexto';
    COMMENT ON COLUMN ai_memory.instance_id IS 'ID da instância WhatsApp';
    COMMENT ON COLUMN ai_memory.role IS 'Papel da mensagem: user, assistant ou system';
    COMMENT ON COLUMN ai_memory.content IS 'Conteúdo da mensagem';
    COMMENT ON COLUMN ai_memory.metadata IS 'Dados adicionais (chat_id, message_id, etc)';

    -- =========================================
    -- LOOP (Fila, totais e configurações por instância)
    -- =========================================
    CREATE TABLE IF NOT EXISTS instance_loop_settings (
      instance_id     TEXT PRIMARY KEY REFERENCES instances(id) ON DELETE CASCADE,
      auto_run        BOOLEAN NOT NULL DEFAULT FALSE,
      ia_auto         BOOLEAN NOT NULL DEFAULT FALSE,
      daily_limit     INTEGER,
      message_template TEXT,
      window_start    TIME NOT NULL DEFAULT '08:00:00',
      window_end      TIME NOT NULL DEFAULT '18:00:00',
      last_run_at     TIMESTAMPTZ,
      loop_status     TEXT NOT NULL DEFAULT 'idle',
      updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS instance_loop_queue (
      id           BIGSERIAL PRIMARY KEY,
      instance_id  TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
      name         TEXT,
      phone        TEXT NOT NULL,
      niche        TEXT,
      source       TEXT NOT NULL DEFAULT 'manual',
      status       TEXT NOT NULL DEFAULT 'pending',
      created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_loop_queue_instance_status
      ON instance_loop_queue(instance_id, status, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_loop_queue_instance_phone
      ON instance_loop_queue(instance_id, phone);

    CREATE TABLE IF NOT EXISTS instance_loop_totals (
      id                BIGSERIAL PRIMARY KEY,
      instance_id       TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
      name              TEXT,
      phone             TEXT NOT NULL,
      niche             TEXT,
      mensagem_enviada  BOOLEAN NOT NULL DEFAULT FALSE,
      status            TEXT DEFAULT 'pending',
      updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS uq_loop_totals_instance_phone
      ON instance_loop_totals(instance_id, phone);
    CREATE INDEX IF NOT EXISTS idx_loop_totals_instance_status
      ON instance_loop_totals(instance_id, mensagem_enviada, updated_at DESC);

    CREATE TABLE IF NOT EXISTS instance_loop_events (
      id           BIGSERIAL PRIMARY KEY,
      instance_id  TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
      event_type   TEXT NOT NULL,
      payload      JSONB DEFAULT '{}'::jsonb,
      created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_loop_events_instance_created
      ON instance_loop_events(instance_id, created_at DESC);
    """
    with get_pool().connection() as con:
        con.execute(sql)
