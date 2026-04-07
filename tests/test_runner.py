"""
Custom test runner that creates FreeRADIUS tables (managed=False) in the test DB.
"""
from django.test.runner import DiscoverRunner
from django.db import connection


RADIUS_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS radcheck (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL DEFAULT '',
    attribute VARCHAR(64) NOT NULL DEFAULT '',
    op VARCHAR(2) NOT NULL DEFAULT ':=',
    value VARCHAR(253) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS radreply (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL DEFAULT '',
    attribute VARCHAR(64) NOT NULL DEFAULT '',
    op VARCHAR(2) NOT NULL DEFAULT '=',
    value VARCHAR(253) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS radusergroup (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL DEFAULT '',
    groupname VARCHAR(64) NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS radgroupcheck (
    id BIGSERIAL PRIMARY KEY,
    groupname VARCHAR(64) NOT NULL DEFAULT '',
    attribute VARCHAR(64) NOT NULL DEFAULT '',
    op VARCHAR(2) NOT NULL DEFAULT ':=',
    value VARCHAR(253) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS radgroupreply (
    id BIGSERIAL PRIMARY KEY,
    groupname VARCHAR(64) NOT NULL DEFAULT '',
    attribute VARCHAR(64) NOT NULL DEFAULT '',
    op VARCHAR(2) NOT NULL DEFAULT ':=',
    value VARCHAR(253) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS radacct (
    radacctid BIGSERIAL PRIMARY KEY,
    acctsessionid VARCHAR(64) NOT NULL DEFAULT '',
    acctuniqueid VARCHAR(32) NOT NULL DEFAULT '' UNIQUE,
    username VARCHAR(64) NOT NULL DEFAULT '',
    realm VARCHAR(64) DEFAULT '',
    nasipaddress VARCHAR(45) NOT NULL DEFAULT '0.0.0.0',
    nasportid VARCHAR(15) DEFAULT '',
    nasporttype VARCHAR(32) DEFAULT '',
    acctstarttime TIMESTAMP WITH TIME ZONE,
    acctupdatetime TIMESTAMP WITH TIME ZONE,
    acctstoptime TIMESTAMP WITH TIME ZONE,
    acctinterval INTEGER,
    acctsessiontime INTEGER,
    acctauthentic VARCHAR(32) DEFAULT '',
    connectinfo_start VARCHAR(50) DEFAULT '',
    connectinfo_stop VARCHAR(50) DEFAULT '',
    acctinputoctets BIGINT,
    acctoutputoctets BIGINT,
    calledstationid VARCHAR(50) DEFAULT '',
    callingstationid VARCHAR(50) DEFAULT '',
    acctterminatecause VARCHAR(32) DEFAULT '',
    servicetype VARCHAR(32) DEFAULT '',
    framedprotocol VARCHAR(32) DEFAULT '',
    framedipaddress VARCHAR(45) NOT NULL DEFAULT '0.0.0.0'
);

CREATE TABLE IF NOT EXISTS radpostauth (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL DEFAULT '',
    pass VARCHAR(64) NOT NULL DEFAULT '',
    reply VARCHAR(32) NOT NULL DEFAULT '',
    authdate TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS nas (
    id BIGSERIAL PRIMARY KEY,
    nasname VARCHAR(128) NOT NULL UNIQUE,
    shortname VARCHAR(32) DEFAULT '',
    type VARCHAR(30) NOT NULL DEFAULT 'other',
    ports INTEGER,
    secret VARCHAR(60) NOT NULL DEFAULT '',
    server VARCHAR(64) DEFAULT '',
    community VARCHAR(50) DEFAULT '',
    description VARCHAR(200) DEFAULT ''
);
"""


class SabiWiFiTestRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        result = super().setup_databases(**kwargs)
        with connection.cursor() as cursor:
            cursor.execute(RADIUS_TABLES_SQL)
        return result

    def run_tests(self, test_labels, **kwargs):
        # SECURE_SSL_REDIRECT=True in prod redirects all HTTP test requests to
        # HTTPS (301), breaking every test. Disable it for the test suite.
        from django.test.utils import override_settings
        with override_settings(SECURE_SSL_REDIRECT=False):
            return super().run_tests(test_labels, **kwargs)
