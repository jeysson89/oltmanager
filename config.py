import os

class Config:
    SECRET_KEY = os.urandom(24).hex()
    SQLALCHEMY_DATABASE_URI = 'mysql+pymysql://oltuser:oltpassword@localhost/oltmanager'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    BILLING_DB_HOST = '31.131.130.40'
    BILLING_DB_PORT = 3306
    BILLING_DB_USER = 'web'
    BILLING_DB_PASSWORD = '151289Nl!'
    BILLING_DB_NAME = 'mikbill'
    BILLING_ADDRESS_QUERY = """SELECT CONCAT(lane, \' \', house,
           IF(app != \'\', CONCAT(\'/\', app), \'\')) AS address
    FROM users_view_fsb_address, dev_fields, dev_user
    WHERE dev_fields.value = \'{mac}\'
    AND dev_fields.devid = dev_user.devid
    AND dev_user.uid = users_view_fsb_address.uid
    LIMIT 1"""
    SNMP_AUTO_SCAN = False
    AUTO_POLL_ENABLED = True
    AUTO_POLL_TIME = "02:00"
    ALLOWED_IPS = []
    MONITORING_ENABLED = True
    MONITORING_INTERVAL = 100
