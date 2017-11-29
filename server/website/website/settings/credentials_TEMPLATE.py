"""
Private/custom Django settings for the OtterTune project.

"""

## ==============================================
## SECRET KEY CONFIGURATION
## ==============================================

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'ADD ME!!'

## ==============================================
## DATABASE CONFIGURATION
## ==============================================

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': 'ottertune', 
        'USER': 'ADD ME!!',
        'PASSWORD': 'ADD ME!!',
        'HOST': '',
        'PORT': '',
        'OPTIONS': {
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES',innodb_strict_mode=1",
        },
    }
}

## ==============================================
## DEBUG CONFIGURATION
## ==============================================

DEBUG = False

## ==============================================
## MANAGER CONFIGURATION
## ==============================================

# Admin and managers for this project. These people receive private
# site alerts.
ADMINS = (
    # ('Your Name', 'your_email@example.com'),
)
MANAGERS = ADMINS

## ==============================================
## GENERAL CONFIGURATION
## ==============================================

# Hosts/domain names that are valid for this site; required if DEBUG is False
# See https://docs.djangoproject.com/en/1.5/ref/settings/#allowed-hosts
ALLOWED_HOSTS = []

## ==============================================
## OTTERTUNE PATH CONFIGURATION
## ==============================================

LOG_FILE = '/path/to/website/log'

OTTERTUNE_LIBS = '/path/to/ottertune/MLlibs'
