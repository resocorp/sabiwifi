"""
FreeRADIUS database tables — unmanaged models.
These tables are created by FreeRADIUS's rlm_sql schema, not Django migrations.
Django reads/writes to them for RADIUS attribute management.
"""
from django.db import models


class Radcheck(models.Model):
    """Per-user check attributes (e.g. password, Simultaneous-Use)."""
    username = models.CharField(max_length=64, default='')
    attribute = models.CharField(max_length=64, default='')
    op = models.CharField(max_length=2, default=':=')
    value = models.CharField(max_length=253, default='')

    class Meta:
        managed = False
        db_table = 'radcheck'
        verbose_name = 'RADIUS Check'
        verbose_name_plural = 'RADIUS Checks'

    def __str__(self):
        return f'{self.username} {self.attribute} {self.op} {self.value}'


class Radreply(models.Model):
    """Per-user reply attributes."""
    username = models.CharField(max_length=64, default='')
    attribute = models.CharField(max_length=64, default='')
    op = models.CharField(max_length=2, default='=')
    value = models.CharField(max_length=253, default='')

    class Meta:
        managed = False
        db_table = 'radreply'
        verbose_name = 'RADIUS Reply'
        verbose_name_plural = 'RADIUS Replies'

    def __str__(self):
        return f'{self.username} {self.attribute} {self.op} {self.value}'


class Radusergroup(models.Model):
    """Maps users to groups (plans)."""
    username = models.CharField(max_length=64, default='')
    groupname = models.CharField(max_length=64, default='')
    priority = models.IntegerField(default=1)

    class Meta:
        managed = False
        db_table = 'radusergroup'
        verbose_name = 'RADIUS User Group'
        verbose_name_plural = 'RADIUS User Groups'

    def __str__(self):
        return f'{self.username} → {self.groupname}'


class Radgroupcheck(models.Model):
    """Per-group check attributes (e.g. Simultaneous-Use)."""
    groupname = models.CharField(max_length=64, default='')
    attribute = models.CharField(max_length=64, default='')
    op = models.CharField(max_length=2, default=':=')
    value = models.CharField(max_length=253, default='')

    class Meta:
        managed = False
        db_table = 'radgroupcheck'
        verbose_name = 'RADIUS Group Check'
        verbose_name_plural = 'RADIUS Group Checks'

    def __str__(self):
        return f'{self.groupname} {self.attribute} {self.op} {self.value}'


class Radgroupreply(models.Model):
    """Per-group reply attributes (e.g. Mikrotik-Rate-Limit, Session-Timeout)."""
    groupname = models.CharField(max_length=64, default='')
    attribute = models.CharField(max_length=64, default='')
    op = models.CharField(max_length=2, default=':=')
    value = models.CharField(max_length=253, default='')

    class Meta:
        managed = False
        db_table = 'radgroupreply'
        verbose_name = 'RADIUS Group Reply'
        verbose_name_plural = 'RADIUS Group Replies'

    def __str__(self):
        return f'{self.groupname} {self.attribute} {self.op} {self.value}'


class Radacct(models.Model):
    """RADIUS accounting records — session data, usage tracking."""
    radacctid = models.BigAutoField(primary_key=True)
    acctsessionid = models.CharField(max_length=64, default='')
    acctuniqueid = models.CharField(max_length=32, default='', unique=True)
    username = models.CharField(max_length=64, default='')
    realm = models.CharField(max_length=64, default='', blank=True)
    nasipaddress = models.GenericIPAddressField(default='0.0.0.0')
    nasportid = models.CharField(max_length=15, default='', blank=True)
    nasporttype = models.CharField(max_length=32, default='', blank=True)
    acctstarttime = models.DateTimeField(null=True, blank=True)
    acctupdatetime = models.DateTimeField(null=True, blank=True)
    acctstoptime = models.DateTimeField(null=True, blank=True)
    acctinterval = models.IntegerField(null=True, blank=True)
    acctsessiontime = models.IntegerField(null=True, blank=True)
    acctauthentic = models.CharField(max_length=32, default='', blank=True)
    connectinfo_start = models.CharField(max_length=50, default='', blank=True)
    connectinfo_stop = models.CharField(max_length=50, default='', blank=True)
    acctinputoctets = models.BigIntegerField(null=True, blank=True)
    acctoutputoctets = models.BigIntegerField(null=True, blank=True)
    calledstationid = models.CharField(max_length=50, default='', blank=True)
    callingstationid = models.CharField(max_length=50, default='', blank=True)
    acctterminatecause = models.CharField(max_length=32, default='', blank=True)
    servicetype = models.CharField(max_length=32, default='', blank=True)
    framedprotocol = models.CharField(max_length=32, default='', blank=True)
    framedipaddress = models.GenericIPAddressField(default='0.0.0.0')

    class Meta:
        managed = False
        db_table = 'radacct'
        verbose_name = 'RADIUS Accounting'
        verbose_name_plural = 'RADIUS Accounting'

    def __str__(self):
        return f'{self.username} session {self.acctsessionid}'


class Radpostauth(models.Model):
    """RADIUS post-authentication log."""
    username = models.CharField(max_length=64, default='')
    password = models.CharField(max_length=64, default='', db_column='pass')
    reply = models.CharField(max_length=32, default='')
    authdate = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = 'radpostauth'
        verbose_name = 'RADIUS Post-Auth'
        verbose_name_plural = 'RADIUS Post-Auth'

    def __str__(self):
        return f'{self.username} {self.reply} at {self.authdate}'


class Nas(models.Model):
    """Network Access Server (NAS) table — one row per router."""
    nasname = models.CharField(max_length=128, unique=True)
    shortname = models.CharField(max_length=32, default='', blank=True)
    type = models.CharField(max_length=30, default='other')
    ports = models.IntegerField(null=True, blank=True)
    secret = models.CharField(max_length=60, default='')
    server = models.CharField(max_length=64, default='', blank=True)
    community = models.CharField(max_length=50, default='', blank=True)
    description = models.CharField(max_length=200, default='', blank=True)

    class Meta:
        managed = False
        db_table = 'nas'
        verbose_name = 'NAS'
        verbose_name_plural = 'NAS'

    def __str__(self):
        return f'{self.nasname} ({self.shortname})'
