#!/usr/bin/env python3
import sys
import json
from datetime import datetime, timedelta, timezone
import os
import subprocess
import re
import pytz
import ipaddress
import struct
import hashlib
import requests
import flask
from flask import Flask, request, make_response, g, jsonify, json, flash, redirect, session, Response
from flask_pymongo import pymongo, PyMongo, ASCENDING, DESCENDING
import pymongo.errors
from flask_wtf import FlaskForm
from flask_mail import Mail, Message
from wtforms import validators, TextField, TextAreaField, FloatField, IntegerField, BooleanField, HiddenField, SelectField, SelectMultipleField, PasswordField
import dateutil.parser
import jinja2
from pymisp import ExpandedPyMISP
import signal
from ipaddress import IPv4Address, AddressValueError

# Add to path the "one directory above the current file location"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))
import common.config
import common.task_queue
from common.utils import ipstr2int, int2ipstr, parse_rfc_time
from shodan_rpc_client import ShodanRpcClient

#import db
import ctrydata
import userdb
import ratelimit
from userdb import get_user_info, authenticate_with_token, generate_unique_token

# ***** Load configuration *****

DEFAULT_CONFIG_FILE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "/etc/nerd/nerdweb.yml"))

# TODO parse arguments using ArgParse
if len(sys.argv) >= 2:
    cfg_file = sys.argv[1]
else:
    cfg_file = DEFAULT_CONFIG_FILE
cfg_dir = os.path.dirname(os.path.abspath(cfg_file))

# Read web-specific config (nerdweb.cfg)
config = common.config.read_config(cfg_file)
# Read common config (nerd.cfg) and combine them together
common_cfg_file = os.path.join(cfg_dir, config.get('common_config'))
config.update(common.config.read_config(common_cfg_file))

# Read tags config (to separate dict)
tags_cfg_file = os.path.join(cfg_dir, config.get('tags_config'))
config_tags = common.config.read_config(tags_cfg_file)

# Read blacklists config (to separate dict)
bl_cfg_file = os.path.join(cfg_dir, config.get('bl_config'))
config_bl = common.config.read_config(bl_cfg_file)

# Read Task queue config
rabbit_config = config.get('rabbitmq')
num_processes = config.get('worker_processes')

# Init Task queue
task_queue_writer = common.task_queue.TaskQueueWriter(num_processes, rabbit_config)
task_queue_writer.connect()


# Create event database driver (according to config)
EVENTDB_TYPE = config.get('eventdb', 'psql')
if EVENTDB_TYPE == 'psql':
    import common.eventdb_psql
    eventdb = common.eventdb_psql.PSQLEventDatabase(config)
elif EVENTDB_TYPE == 'mentat':
    import common.eventdb_mentat
    eventdb = common.eventdb_mentat.MentatEventDBProxy(config)
else:
    EVENTDB_TYPE = 'none'
    print("ERROR: unknown 'eventdb' configured, it will not be possible to show raw events in GUI", file=sys.stderr)

try:
    misp_inst = ExpandedPyMISP(config['misp']['url'], config['misp']['key'], None)
except KeyError:
    misp_inst = None

MISP_THREAT_LEVEL_DICT = {'1': "High", '2': "Medium", '3': "Low", '4': "Undefined"}
MISP_ANALYSIS = ["Initial", "Ongoing", "Completed"]
MISP_DISTRIBUTION = ["Your Organisation Only", "This Community Only", "Connected Communities", "All Communities", "Sharing Group"]

BASE_URL = config.get('base_url', '')

WARDEN_DROP_PATH = os.path.join(config.get("warden_filer_path", "/data/warden_filer/warden_receiver"), "incoming")

config.testing = False

userdb.init(config, cfg_dir)

# **** Create and initialize Flask application *****

app = Flask(__name__)

app.secret_key = config.get('secret_key')

app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True

# Configuration of PyMongo
app.config['MONGO_URI'] = "mongodb://{}:{}/{}".format(
    config.get('mongodb.host', 'localhost'),
    config.get('mongodb.port', 27017),
    config.get('mongodb.dbname', 'nerd')
)

mongo = PyMongo(app)

# Configuration of MAIL extension
app.config['MAIL_SERVER'] = config.get('mail.server', 'localhost')
app.config['MAIL_PORT'] = config.get('mail.port', '25')
app.config['MAIL_USE_TLS'] = config.get('mail.tls', False)
app.config['MAIL_USE_SSL'] = config.get('mail.ssl', False)
app.config['MAIL_USERNAME'] = config.get('mail.username', None)
app.config['MAIL_PASSWORD'] = config.get('mail.password', None)
app.config['MAIL_DEFAULT_SENDER'] = config.get('mail.sender', 'NERD <noreply@nerd.example.com>')

mailer = Mail(app)

# Disable CSRF protection globally (it's OK to send search requests from anywhere)
# FIXME: This disables it completeley, it would be better to rather disable it
# by default but keep an option to check CSRF explicitly in selected Forms.
# This should be possible by setting WTF_CSRF_CHECK_DEFAULT, but it doesn't
# work for me (form.validate fails then)
app.config['WTF_CSRF_ENABLED'] = False
#app.config['WTF_CSRF_CHECK_DEFAULT'] = False


# ***** Jinja2 filters *****

# Datetime filters
def format_datetime(val, format="%Y-%m-%d %H:%M:%S"):
    return val.strftime(format)


def is_date(val):
    if type(val) is datetime:
        return True
    if isinstance(val, str):
        try:
            _ = dateutil.parser.parse(timestr=val)
            return True
        except ValueError:
            return False
    return False


def date_to_int(val):
    if type(val) is datetime:
        return val.replace(tzinfo=timezone.utc).timestamp()
    if isinstance(val, str):
        date_value = dateutil.parser.parse(timestr=val)
        return date_value.replace(tzinfo=timezone.utc).timestamp()


def timestamp_to_date(timestamp):
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)


def misp_threat_level_id_to_str(threat_level_id):
    return MISP_THREAT_LEVEL_DICT[threat_level_id]


def misp_analysis_id_to_str(analysis_id):
    return MISP_ANALYSIS[int(analysis_id)]


def misp_distribution_id_to_str(distribution_id):
    return MISP_DISTRIBUTION[int(distribution_id)]


def misp_sightings_to_str(sightings):
    if sightings is not None:
        sightings_count = [0, 0, 0]
        for sighting_record in sightings:
            sightings_count[int(sighting_record['type'])] += 1
        return '/'.join([str(count) for count in sightings_count])
    else:
        return "0/0/0"


def is_ip_address(value):
    try:
        _ = IPv4Address(value)
        return True
    except AddressValueError:
        return False


def misp_contains_ip_address(value, attrib_type, get_rest=False):
    """
    Checks if string value contains IP address in it and returns it
    :param value: Checked string
    :param attrib_type: type of MISP attribute
    :param get_rest: If True, then do not return IP address, but the rest of the string (except delimiter)
    :return: IP address if get_rest is False, otherwise returns the rest of the string
    """
    if '|' in value or ':' in value:
        try:
            # if domain in attribute's type, the type is domain|ip, so ip is on index 1, else it is ip-src|port or
            # ip-dst|port
            _ = IPv4Address(value.split('|')[1] if "domain" in attrib_type else value.split('|')[0])
            if "domain" in attrib_type:
                return value.split('|')[1] if not get_rest else value.split('|')[0]
            else:
                return value.split('|')[0] if not get_rest else value.split('|')[1]
        except AddressValueError:
            try:
                # ':' appears only in ip-src|port or ip-dst|port, there is position of ip address in string strict
                _ = IPv4Address(value.split(':')[0])
                return value.split(':')[0] if not get_rest else value.split(':')[1]
            except AddressValueError:
                return False
    return False


def misp_get_tags(tag_list):
    return [tag['name'] for tag in tag_list]


def misp_get_cluster_count(event):
    counter = 0
    for galaxy in event.get('Galaxy', []):
        for _ in galaxy['GalaxyCluster']:
            counter += 1
    return counter


app.jinja_env.filters['datetime'] = format_datetime
app.jinja_env.filters['is_date'] = is_date
app.jinja_env.filters['date_to_int'] = date_to_int
app.jinja_env.filters['timestamp_to_date'] = timestamp_to_date
app.jinja_env.filters['misp_threat_level_id_to_str'] = misp_threat_level_id_to_str
app.jinja_env.filters['misp_analysis_id_to_str'] = misp_analysis_id_to_str
app.jinja_env.filters['misp_distribution_id_to_str'] = misp_distribution_id_to_str
app.jinja_env.filters['misp_sightings_to_str'] = misp_sightings_to_str
app.jinja_env.filters['is_ip_address'] = is_ip_address
app.jinja_env.filters['misp_contains_ip_address'] = misp_contains_ip_address
app.jinja_env.filters['misp_get_tags'] = misp_get_tags
app.jinja_env.filters['misp_get_cluster_count'] = misp_get_cluster_count


# ***** WTForm validators and filters *****

# Own reimplamentation of wtforms.validators.Optional
# The original one uses field.raw_data, but we need field.data
# (raw_data only contain data loaded from Form (GET/POST), not those passed
#  to Form constructor using obj/data/kwargs parameters)
def validator_optional(form, field):
    if not field.data:
        field.errors[:] = []
        raise validators.StopValidation()

# Filter to strip whitespaces in string
def strip_whitespace(s):
    if isinstance(s, str):
        s = s.strip()
    return s


# ***** Auxiliary functions *****

def pseudonymize_node_name(name):
    """Replace Node.Name (detector ID) by a hash with secret key"""
    h = hashlib.md5((app.secret_key + name).encode('utf-8'))
    return 'node.' + h.hexdigest()[:6]


# ***** Rate limiter *****

def get_rate_limit_params(userid):
    if userid.startswith("ip:"):
        return None
    assert(g.user['fullid'] == userid)
    bs = g.user['rl_bs']
    tps = g.user['rl_tps']
    if bs is None or tps is None:
        return None
    return bs, tps

rate_limiter = ratelimit.RateLimiter(config, get_rate_limit_params)


# ***** Login handlers *****
# A handler function is created for each configured login method
# (config.login.<method>) with URL path set to config.login.<method>.loc.
# 
# Each of these paths should have a login mechanism configured in web server.
# (e.g. HTTP basic authentication or Shibboleth).
# The handler expect a valid user information in environment variables set by
# the server.
# The user info (id and optionally name and email) is taken from environment 
# variables specified in config.login.<method>.{id_field,user_field,email_field}. 

def create_login_handler(method_id, id_field, name_field, email_field, return_path):
    def login_handler():
        # DEBUG: print whole environ to see what fields IdP has provided
        #if method_id == 'shibboleth':
        #    print("Shibboleth login, metadata provided: "+str(request.environ))
        
        # Check presence of the only mandatory field (id)
        if id_field not in request.environ:
            flash("ERROR: Login failed - '"+id_field+"' not defined (either your IdP is not providing this field or there is a problem with server configuration).", "error")
            return redirect(return_path)
        session['user'] = {
            'login_type': method_id,
            'id': request.environ[id_field],
        }
        # Name may be present in various fields, try all specified in the config and use the first present
        for field in (name_field if isinstance(name_field, list) else ([name_field] if name_field else [])):
            # Name may be a combination of more fields, specified using "+" symbol (e.g. )
            if "+" in field and all(f in request.environ for f in field.split('+')):
                session['user']['name'] = " ".join(map(lambda f: request.environ[f], field.split('+')))
                break
            elif field in request.environ:
                session['user']['name'] = request.environ[field]
                break
        # Decode name from UTF-8 (Flask returns environ fields as str (which is always Unicode in Py3), but not parsed as utf-8; convert to bytes and decode)
        if 'name' in session['user']:
            session['user']['name'] = bytes(session['user']['name'], 'latin-1').decode('utf-8')
        # Email
        if email_field and email_field in request.environ:
            session['user']['email'] = request.environ[email_field]
        flash("Login successful", "success")
        return redirect(return_path)
    return login_handler

# Register the login handlers
for method_id, method_cfg in config.get('login.methods', {}).items():
    app.add_url_rule(
        method_cfg['loc'],
        'login_'+method_id,
        create_login_handler(method_id,
                             method_cfg.get('id_field', 'REMOTE_USER'),
                             method_cfg.get('name_field', None),
                             method_cfg.get('email_field', None),
                             BASE_URL+'/'
        )
    )

# Devel login
# (logs in 'developer' automatically, only available if g.testing is enabled)
@app.route('/login/devel')
def login_devel():
    if not config.testing:
        return flask.abort(404)
    session['user'] = {
        'login_type': 'devel',
        'id': 'devel_admin',
    }
    return redirect(BASE_URL+'/')


@app.route('/logout')
def logout():
    redir_path = BASE_URL+'/'
    if 'user' in session:
        # If there is logout-path defined for the login_type, redirect to this instead of the default
        login_type = session['user']['login_type']
        if login_type != 'devel' and 'logout_path' in config['login']['methods'][login_type]:
            redir_path = config['login']['methods'][login_type]['logout_path']
        # Cancel local NERD session
        del session['user']
        flash("You have been logged out", "info")
    return redirect(redir_path)


# ***** Functions called for each request *****

API_RESPONSE_403_NOAUTH = Response(
    json.dumps({'err_n' : 403, 'error' : "Unauthorized (no authorization header)"}),
    403, mimetype='application/json'
)
API_RESPONSE_403_TOKEN = Response(
    json.dumps({'err_n' : 403, 'error' : "Unauthorized (invalid token)"}),
    403, mimetype='application/json'
)
API_RESPONSE_403 = Response(
    json.dumps({'err_n' : 403, 'error' : "Unauthorized (not authorized to use this endpoint)"}),
    403, mimetype='application/json'
)


@app.before_request
def store_user_info():
    """Store user info to 'g' (request-wide global variable)"""
    if request.path.endswith("/test"):
        return

    if request.path.startswith("/api/v1/"):
        # API authentication using token
        auth = request.headers.get("Authorization")
        if not auth:
            return API_RESPONSE_403_NOAUTH

        # Extract token from Authorization header. Two formats may be used:
        #   Authorization: asdf1234qwer
        #   Authorization: token asdf1234qwer
        vals = auth.split()
        if len(vals) == 1:
            token = vals[0]
        elif len(vals) == 2 and vals[0] == "token":
            token = vals[1]
        else:
            return API_RESPONSE_403_TOKEN

        g.user, g.ac = authenticate_with_token(token)
        if not g.user:
            return API_RESPONSE_403_TOKEN

    else:
        # Normal authentication using session cookie
        g.user, g.ac = get_user_info(session)


def exceeded_rate_limit(user_id):
    if request.path.startswith("/api"):
        # API -> return error in JSON
        err = {
            'err_n': 429,
            'error': "Too many requests",
        }
        return Response(json.dumps(err), 429, mimetype='application/json')
    else:
        # Web -> return HTML with more information
        bs, tps = rate_limiter.get_user_params(user_id)
        if g.user:
            message = "You are only allowed to make {} requests per second.".format(tps)
        else:
            message = "We only allow {} requests per second per IP address for not logged in users.".format(tps)
        return make_response(render_template('429.html', message=message), 429)


def get_user_id():
    # Get user ID (username or IP address)
    if g.user:
        return g.user['fullid']
    else:
        return 'ip:' + request.remote_addr


@app.before_request
def rate_limit():
    """Check if user hasn't exceeded its rate-limit"""
    try:
        # Ignore requests in some paths
        if request.path.startswith("/static/") or request.path.startswith("/login/") or request.path == "/logout":
            return None
        user_id = get_user_id()
        # TODO set different cost for some endpoints
        ok = rate_limiter.try_request(user_id, cost=1)
        if not ok:
            # Rate-limit exceeded, return error message
            return exceeded_rate_limit(user_id)
    except Exception as e:
        # If anything fails, log error and continue - web shouldn't stop working
        # just because an error in the rate-limiter
        print("RateLimit error:", e)
    return None

@app.after_request
def add_user_header(resp):
    # Set user ID to a special header, it's used to put user ID to Apache logs
    try:
        resp.headers['X-UserID'] = g.user['fullid']
    except (AttributeError, KeyError, TypeError):
        pass
    return resp


# ***** Override render_template to always include some variables *****

def render_template(template, **kwargs):
    return flask.render_template(template, config=config, config_tags=config_tags['tags'], userdb=userdb, user=g.user, ac=g.ac, **kwargs)


# ***** Main page *****
# TODO: rewrite as before_request (to check for this situation at any URL)
@app.route('/')
def main():
    # User is authenticated but has no account
    if g.user and g.ac('notregistered'):
        return redirect(BASE_URL+'/noaccount')
    
    return redirect(BASE_URL+'/ips/')


# ***** Request for new account *****
class AccountRequestForm(FlaskForm):
    email = TextField('Contact email', [validators.Required()], description='Used to send information about your request and in case admins need to contact you.')
    message = TextAreaField("", [validators.Optional()])
    action = HiddenField('action')

@app.route('/noaccount', methods=['GET','POST'])
def noaccount():
    if not g.user:
        return make_response("ERROR: no user is authenticated")
    if not g.ac('notregistered'):
        return redirect(BASE_URL+'/ips/')
    if g.user['login_type'] != 'shibboleth':
        return make_response("ERROR: You've successfully authenticated to web server but there is no matching user account. This is probably a configuration error. Contact NERD administrator.")
    
    form = AccountRequestForm(request.values)
    # Prefill user's default email from his/her account info (we expect a list of emails separated by ';')
    if not form.email.data and 'email' in g.user:
        form.email.data = g.user['email'].split(';')[0]
    
    request_sent = False
    if form.validate() and form.action.data == 'request_account':
        # Check presence of config login.request-email
        if not config.get('login.request-email', None):
            return make_response("ERROR: No destination email address configured. This is a server configuration error. Please, report this to NERD administrator if possible.")
        # Send email
        name = g.user.get('name', '[name not available]')
        id = g.user['id']
        email = form.email.data
        message = form.message.data
        msg = Message(subject="[NERD] New account request from {} ({})".format(name,id),
                      recipients=[config.get('login.request-email')],
                      reply_to=email,
                      body="A user with the following ID has requested creation of a new account in NERD.\n\nid: {}\nname: {}\nemails: {}\nselected email: {}\n\nMessage:\n{}".format(id,name,g.user.get('email',''),email,message),
                     )
        mailer.send(msg)
        request_sent = True
        
    return render_template('noaccount.html', **locals())


# ***** Account info & password change *****

class PasswordChangeForm(FlaskForm):
    old_passwd = PasswordField('Old password', [validators.InputRequired()])
    new_passwd = PasswordField('New password', [validators.InputRequired(), validators.length(8, -1, 'Password must have at least 8 characters')])
    new_passwd2 = PasswordField('Repeat password', [validators.InputRequired(), validators.EqualTo('new_passwd', message='Passwords must match')])


@app.route('/account')
@app.route('/account/gen_token', endpoint='gen_token', methods=['POST'])
@app.route('/account/set_password', endpoint='set_password', methods=['POST'])
def account_info():
    if not g.user:
        return make_response("ERROR: no user is authenticated")
    if g.user and g.ac('notregistered'):
        return redirect(BASE_URL+'/noaccount')

    if request.endpoint == 'gen_token':
        if not generate_unique_token(g.user):
            return make_response("ERROR: An unexpected error during token creation occured.")
        return redirect(BASE_URL+'/account')

    token = {}
    if not g.user['api_token']:
        token['value'] = 'Token not created yet.'
        token['status'] = 0
    else:
        token['value'] = g.user['api_token']
        token['status'] = 1

    # Handler for /account/set_password
    if request.endpoint == 'set_password':
        if g.user['login_type'] != 'local':
            return make_response("ERROR: Password can be changed for local accounts only")
        passwd_form = PasswordChangeForm(request.form)
        if passwd_form.validate():
            htpasswd_file = os.path.join(cfg_dir, config.get('login.methods.local.htpasswd_file', '.htpasswd'))
            try:
                # Verify old password
                cmd = ['htpasswd', '-v', '-i', htpasswd_file, g.user['id']]
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE)#, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                p.communicate(passwd_form.old_passwd.data.encode('utf-8'))
                if p.returncode != 0:
                    if p.returncode == 3: # Bad password
                        flash('ERROR: Bad password', 'error')
                        return render_template('account_info.html', **locals())
                    else:
                        print("ERROR: htpasswd check error '{}': retcode: {}".format(' '.join(cmd), p.returncode), file=sys.stderr)
                        return make_response('ERROR: Cannot change password: error '+str(p.returncode),  'error')
                
                # Set new password
                # (-i: read password from stdin, -B: use bcrypt, -C: bcrypt cost factor (12 should be quite secure and takes approx. 0.3s on my server)
                cmd = ['htpasswd', '-i', '-B', '-C', '12', htpasswd_file, g.user['id']]
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE)#, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                p.communicate(passwd_form.new_passwd.data.encode('utf-8'))
                if p.returncode != 0:
                    print("ERROR: htpasswd set error '{}': retcode: {}".format(' '.join(cmd), p.returncode), file=sys.stderr)
                    return make_response('ERROR: Cannot change password: error '+str(p.returncode),  'error')
            except OSError as e:
                p.kill()
                print("ERROR: htpasswd OSError '{}': {}".format(' '.join(cmd), str(e)), file=sys.stderr)
                return make_response('ERROR: Cannot change password: OSError.', 'error')
            
            # If we got there, password was successfully changed
            flash('Password changed. Please, <b><a href="'+BASE_URL+'/logout">log out</a></b> and then log back in using the new password.', 'safe success')
            return redirect(BASE_URL+'/account')
        else:
            flash('ERROR: Password not changed.', 'error')

    # Handler for /account
    else:
        if g.user['login_type'] == 'local':
            passwd_form = PasswordChangeForm()
    
    return render_template('account_info.html', **locals())


# ***** Admin's selection of effective groups *****

# Called via AJAX, only sets parameters in session (page should be reloaded 
# by JS after successful call of this).
# Expects one parameter: groups=grp1,grp2,grp3
# If no parameter is passed, selected_groups are reset to normal set of groups of the user
@app.route('/set_effective_groups')
def set_effective_groups():
    # Only admin can change groups (check group membership, not ac() func since it uses effective groups which may be different)
    if 'admin' not in g.user['groups']:
        return Response('Unauthorized', 403, mimetype='text/plain')
    
    if 'groups' in request.args:
        # Set selected groups
        session['user']['selected_groups'] = request.args['groups'].split(',')
    elif 'selected_groups' in session['user']:
        # Reset selected groups
        del session['user']['selected_groups']
    session.modified = True
    return Response('OK', 200, mimetype='text/plain')



# ***** List of IP addresses *****

def get_ip_blacklists():
    # Get the list of all configured IP blacklists. Return array of (id, name).
    # DNSBL (IP only)
    blacklists = [(bl_name, bl_name) for bl_group in config.get('dnsbl.blacklists', []) for bl_name in bl_group[2].values()]
    # Blacklists cached in Redis (IP and prefix)
    blacklists += [(bl[0], bl[1]) for bl in config_bl.get('iplists', [])]
    blacklists += [(bl[0], bl[1]) for bl in config_bl.get('prefixiplists', [])]
    blacklists.sort()
    return blacklists

def get_domain_blacklists():
    # Get the list of all configured domain blacklists. Return array of (id, name).
    blacklists = [(bl[0], bl[1]) for bl in config_bl.get('domainlists', [])]
    blacklists.sort()
    return blacklists

def get_tags():
    """Get list of all configured tags (list of IDs and names)"""
    tags = [ (tag_id, tag_param.get('name', tag_id)) for tag_id, tag_param in config_tags.get('tags', {}).items()]
    tags.sort()    
    return tags

def subnet_validator(form, field):
    try:
        ipaddress.IPv4Network(field.data, strict=False)
    except ValueError:
        raise validators.ValidationError()

class IPFilterForm(FlaskForm):
    subnet = TextField('IP prefix', [validators.Optional(), subnet_validator], filters=[strip_whitespace])
    hostname = TextField('Hostname suffix', [validators.Optional()], filters=[strip_whitespace])
    country = TextField('Country code', [validators.Optional(), validators.length(2, 2)], filters=[strip_whitespace])
    asn = TextField('ASN', [validators.Optional(),
        validators.Regexp('^(AS)?\d+$', re.IGNORECASE,
        message='Must be a number, optionally preceded by "AS".')], filters=[strip_whitespace])
    cat = SelectMultipleField('Event category', [validators.Optional()]) # Choices are set up dynamically (see below)
    cat_op = HiddenField('', default="or")
    node = SelectMultipleField('Node', [validators.Optional()])
    node_op = HiddenField('', default="or")
    blacklist = SelectMultipleField('Blacklist', [validators.Optional()])
    bl_op = HiddenField('', default="or")
    tag_op = HiddenField('', default="or")
    tag = SelectMultipleField('Tag', [validators.Optional()],
        choices = get_tags()
        )
    tag_conf = FloatField('Min tag confidence', [validators.Optional(), validators.NumberRange(0, 1, 'Must be a number between 0 and 1')], default=0.5)
    sortby = SelectField('Sort by', choices=[
                ('none',"--"),
                ('rep','Reputation score'),
                ('events','Events'),
                ('last_activity','Time of last event'),
                ('ts_added','Time added'),
                ('ip','IP address'),
             ], default='rep')
    asc = BooleanField('Ascending', default=False)
    limit = IntegerField('Max number of addresses', [validators.NumberRange(1, 1000)], default=20)
    
    # Choices for some lists must be loaded dynamically from DB, so they're
    # defined when FlaskForm is initialized
    def __init__(self, *args, **kwargs):
        super(IPFilterForm, self).__init__(*args, **kwargs)
        # Dynamically load list of Categories/Nodes and their number of occurrences
        # Collections n_ip_by_* should be periodically updated by queries run by 
        # cron (see /scripts/update_db_meta_info.js)
        self.cat.choices = [(item['_id'], '{} ({})'.format(item['_id'], int(item['n']))) for item in mongo.db.n_ip_by_cat.find().sort('_id') if item['_id']]
        self.node.choices = [(item['_id'], '{} ({})'.format(item['_id'], int(item['n']))) for item in mongo.db.n_ip_by_node.find().sort('_id') if item['_id']]
        # Number of occurrences for blacklists (list of blacklists is taken from configuration)
        bl_name2num = {item['_id']: int(item['n']) for item in mongo.db.n_ip_by_bl.find()}
        dbl_name2num = {item['_id']: int(item['n']) for item in mongo.db.n_ip_by_dbl.find()}
        bl_choices = [('i:'+id, '[IP] {} ({})'.format(name, bl_name2num.get(id, 0))) for id,name in get_ip_blacklists()]
        dbl_choices = [('d:'+id, '[dom] {} ({})'.format(name, dbl_name2num.get(id, 0))) for id,name in get_domain_blacklists()]
        self.blacklist.choices = bl_choices + dbl_choices

class IPFilterFormUnlimited(IPFilterForm):
    """Subclass of IPFilterForm with possibility to set no limit on number of results (used by API)"""
    limit = IntegerField('Max number of addresses', [validators.Optional()], default=20)

class IPFilterFormUnlimitedDef(IPFilterForm):
    """Subclass of IPFilterForm with possibility to set no limit on number of results and no limit is default(used by API)"""
    limit = IntegerField('Max number of addresses', [validators.Optional()], default=0)


sort_mapping = {
    'none': 'none',
    'rep': 'rep',
    'events': 'events_meta.total',
    'last_activity': 'last_activity',
    'ts_added': 'ts_added',
    'ip': '_id',
}

def create_query(form):
    # Prepare 'find' part of the query
    queries = []
    if form.subnet.data:
        subnet = ipaddress.IPv4Network(form.subnet.data, strict=False)
        form.subnet.data = str(subnet) # Convert to canonical form (e.g. 1.2.3.4/16 -> 1.2.0.0/16)
        subnet_start = int(subnet.network_address) # IP addresses are stored as int
        subnet_end = int(subnet.broadcast_address)
        queries.append( {'$and': [{'_id': {'$gte': subnet_start}}, {'_id': {'$lte': subnet_end}}]} )
    if form.hostname.data:
        hn = form.hostname.data[::-1] # Hostnames are stored reversed in DB to allow search by suffix as a range search
        hn_end = hn[:-1] + chr(ord(hn[-1])+1)
        queries.append( {'$and': [{'hostname': {'$gte': hn}}, {'hostname': {'$lt': hn_end}}]} )
    if form.country.data:
        queries.append( {'geo.ctry': form.country.data.upper() } )
    if form.asn.data and form.asn.data.strip():
        # ASN is not stored in IP records - get list of BGP prefixes of the ASN and filter by these
        asn = int(form.asn.data.lstrip("ASas"))
        asrec = mongo.db.asn.find_one({'_id': asn})
        if asrec and 'bgppref' in asrec:
            queries.append( {'bgppref': {'$in': asrec['bgppref']}} )
        else:
            queries.append( {'_id': {'$exists': False}} ) # ASN not in DB, add query which is always false to get no results
    if form.cat.data:
        op = '$and' if (form.cat_op.data == "and") else '$or'
        queries.append( {op: [{'events.cat': cat} for cat in form.cat.data]} )
    if form.node.data:
        op = '$and' if (form.node_op.data == "and") else '$or'
        queries.append( {op: [{'events.node': node} for node in form.node.data]} )
    if form.blacklist.data:
        op = '$and' if (form.bl_op.data == "and") else '$or'
        array = [{('dbl' if t == 'd' else 'bl'): {'$elemMatch': {'n': id, 'v': 1}}} for t,_,id in map(lambda s: s.partition(':'), form.blacklist.data)]
        queries.append( {op: array} )
    if form.tag.data:
        op = '$and' if (form.tag_op.data == "and") else '$or'
        confidence = form.tag_conf.data if form.tag_conf.data else 0
        queries.append( {op: [{'$and': [{'tags.'+ tag_id: {'$exists': True}}, {'tags.'+ tag_id +'.confidence': {'$gte': confidence}}]} for tag_id in form.tag.data]} )
    query = {'$and': queries} if queries else None
    return query

@app.route('/ips')
@app.route('/ips/')
def ips():
    title = "IP search"
    form = IPFilterForm(request.args)
    cfg_max_event_history = config.get('max_event_history', '?')

    # Disallow to see/search by 'misp_tlp_green' tag if the user doesn't have the 'tlp-green' permission
    if not g.ac('tlp-green'):
        form.tag.choices = [(tag_id, tag_name) for tag_id, tag_name in form.tag.choices if tag_id != 'misp_tlp_green']

    if g.ac('ipsearch') and form.validate():
        tz_utc = pytz.utc 
        timezone = pytz.timezone('Europe/Prague') # TODO autodetect (probably better in javascript)
        sortby = sort_mapping[form.sortby.data]
        
        try:
            query = create_query(form)
            # Query parameters to be used in AJAX requests
            query_params = json.dumps(form.data)
        
            # Perform DB query
            results = mongo.db.ip.find(query).limit(form.limit.data)
            if sortby != "none":
                results.sort(sortby, 1 if form.asc.data else -1)
            results = list(results) # Load all data now, so we are able to get number of results in template
        except pymongo.errors.ServerSelectionTimeoutError:
            results = []
            error = 'database_error'

        # Convert _id from int to dotted-decimal string        
        for ip in results:
            ip['_id'] = int2ipstr(ip['_id'])
        
        for ip in results:    
            if "bgppref" in ip:
                asn_list = []
                bgppref = mongo.db.bgppref.find_one({'_id':ip['bgppref']})
                if not bgppref or 'asn' not in bgppref:
                    continue # an inconsistence in DB, it may happen temporarily

                for i in  bgppref['asn']:
                    i = mongo.db.asn.find_one({'_id':i})
                    if not i or 'bgppref' not in i:
                        continue
                    del i['bgppref']
                    asn_list.append(i)

                del bgppref['asn']
                ip['bgppref'] = bgppref
                ip['asn'] = asn_list

        # Add metainfo about events for easier creation of event table in the template
        for ip in results:
            events = ip.get('events', [])
            # Get sets of all dates, cats and nodes
            dates = set()
            cats = set()
            nodes = set()
            for evtrec in events:
                dates.add(evtrec['date'])
                cats.add(evtrec['cat'])
                nodes.add(evtrec['node'])
            try:
                nodes.remove('?')
            except KeyError:
                pass

            # Pseudonymize node names if user is not allowed to see the original names
            if not g.ac('nodenames'):
                nodes = [pseudonymize_node_name(name) for name in nodes]

            dates = sorted(dates)
            cats = sorted(cats)
            nodes = sorted(nodes)
            
            # Show only last 5 days
            MAX_DAYS = 5
            ellipsis = False
            if len(dates) > MAX_DAYS:
                dates = dates[-MAX_DAYS:]
                ellipsis = True
            
            # Table len(dates) x len(cats) -> number
            date_cat_table = [ [0 for _ in cats] for _ in dates ] 
            for evtrec in events:
                try:
                    date_cat_table[dates.index(evtrec['date'])][cats.index(evtrec['cat'])] += evtrec['n']
                except ValueError:
                    pass # date not found in dates because we cut it
            
            # Insert ellipsis at the beginning of the table to show there are more data in older dates
            if ellipsis:
                dates.insert(0, '...')
                date_cat_table.insert(0, ['...' for _ in cats])
            
            # Store info into IP record
            ip['_evt_info'] = {
                'dates': ','.join(dates),
                'cats': ','.join(cats),
                'nodes': ','.join(nodes),
                'date_cat_table': ';'.join( [','.join(map(str,c)) for c in date_cat_table] ),
                'n_cats': len(cats),
                'n_nodes': len(nodes)
            }
        
            # Add number of "visible" MISP events (i.e. after filtering by TLP and user's access rights)
            showable_misp_events = 0
            for misp_event in ip.get('misp_events', []):
                tlp = misp_event.get('tlp')
                if tlp == "white" or (tlp == "green" and g.ac('tlp-green')):
                    showable_misp_events += 1
            ip['_showable_misp_events'] = showable_misp_events
        
    else:
        results = None
        if g.user and not g.ac('ipsearch'):
            flash('Insufficient permissions to search/view IPs.', 'error')
    
    return render_template('ips.html', json=json, ctrydata=ctrydata, **locals())


@app.route('/_ips_count', methods=['POST'])
def ips_count():
    #Excepts query as JSON encoded POST data.
    form_values = request.get_json()
    form = IPFilterForm(obj=form_values)
    if g.ac('ipsearch') and form.validate():
        query = create_query(form)
        #print("query: " + str(query))
        return make_response(str(mongo.db.ip.find(query).count()))
    else:
        return make_response("ERROR")



# ***** Detailed info about individual IP *****

class SingleIPForm(FlaskForm):
    ip = TextField('IP address', [validator_optional, validators.IPAddress(message="Invalid IPv4 address")], filters=[strip_whitespace])

@app.route('/ip/')
@app.route('/ip/<ipaddr>')
def ip(ipaddr=None):
    # Validate IP address
    form = SingleIPForm(data={'ip':ipaddr})
    if form.validate():
        ipaddr = form.ip.data # get IP back from Form to apply filters (strip whitespace).
    else:
        flash('Invalid IPv4 address', 'error')
        ipaddr = None

    if ipaddr:
        if g.ac('ipsearch'):
            title = ipaddr
            ipnum = ipstr2int(ipaddr)
            ipinfo = mongo.db.ip.find_one({'_id': ipnum})
            
            asn_list = []
            if ipinfo:
                if 'bgppref' in ipinfo:
                    bgppref = mongo.db.bgppref.find_one({'_id': ipinfo['bgppref']})
                    if bgppref and 'asn' in bgppref:
                        for asn in bgppref['asn']:
                            asn = mongo.db.asn.find_one({'_id': asn})
                            if not asn or 'bgppref' not in asn:
                                continue
                            #del asn['bgppref']
                            asn_list.append(asn)
                ipinfo['asns'] = asn_list
            
                # Pseudonymize node names if user is not allowed to see the original names
                if not g.ac('nodenames'):
                    for evtrec in ipinfo.get('events', []):
                        evtrec['node'] = pseudonymize_node_name(evtrec['node'])
        else:
            flash('Insufficient permissions to search/view IPs.', 'error')
    else:
        title = 'IP detail search'
        ipinfo = {}
    return render_template('ip.html', ctrydata=ctrydata, ip=ipaddr, **locals())

# Functions to asynchornously request creation of a new IP record
# We use special endpoints, called by JavaScript, since that way we can easily disallow this functionality for robots
# by blocking /ajax/* in robots.txt
@app.route('/ajax/fetch_ip_data/<ipaddr>')
def ajax_request_ip_data(ipaddr):
    """Request backend to create a new short-life record (3 hours) of IP address to get its basic info.

    This cost 10 rate-limit tokens (9 are taken here since 1 is taken in @before_request)
    """
    user_id = get_user_id()
    ok = rate_limiter.try_request(user_id, cost=9)
    if not ok:
        return exceeded_rate_limit(user_id)

    record_ttl = datetime.utcnow() + timedelta(hours=3)
    task_queue_writer.put_task('ip', ipaddr, [('set', '_ttl.web', record_ttl)], priority=True)
    return make_response("OK")


@app.route('/ajax/is_ip_prepared/<ipaddr>')
def ajax_is_ip_prepared(ipaddr):
    try:
        ipaddress.IPv4Address(ipaddr)
    except AddressValueError:
        return Response(json.dumps({'err_n' : 400, 'error' : "Invalid IP address"}), 400, mimetype='application/json')

    ipnum = ipstr2int(ipaddr)
    ipinfo = mongo.db.ip.find_one({'_id': ipnum})

    if ipinfo:
        return "true"
    else:
        return "false"

@app.route('/ajax/ip_events/<ipaddr>')
def ajax_ip_events(ipaddr):
    """Return events related to given IP (as HTML snippet to be loaded via AJAX)"""

    if not ipaddr:
        return make_response('ERROR')
    if not g.ac('ipsearch'):
        return make_response('ERROR: Insufficient permissions')

    events = []
    error = None
    
    # Get only data from last 14 days
    ip_lifetime = config.get('inactive_ip_lifetime', 14)
    from_date = datetime.utcnow() - timedelta(days=ip_lifetime)
    
    # PSQL database
    if EVENTDB_TYPE == 'psql':
        events = eventdb.get('ip', ipaddr, limit=100, dt_from=from_date)
    # Mentat
    elif EVENTDB_TYPE == 'mentat':
        try:
            events = eventdb.get('ip', ipaddr, limit=100, dt_from=from_date)
        except (common.eventdb_mentat.NotConfigured,common.eventdb_mentat.GatewayError) as e:
            error = 'ERROR: ' + str(e)
    # no database to read events from
    else:
        error = 'Event database disabled'

    # Compute "duration" for each event
    for event in events:
        start = end = None
        try:
            if event.get("EventTime") and event.get("CeaseTime"):
                start = parse_rfc_time(event['EventTime'])
                end = parse_rfc_time(event['CeaseTime'])
            elif event.get("WinStartTime") and event.get("WinEndTime"):
                start = parse_rfc_time(event['WinStartTime'])
                end = parse_rfc_time(event['WinEndTime'])
        except ValueError:
            pass # Invalid format of some time specification
        if start and end:
            event["_duration"] = (end - start).total_seconds()

    num_events = str(len(events))
    if len(events) >= 100:
        num_events = "&ge;100, only latest 100 shown"
    return render_template('ip_events.html', json=json, **locals())


# ***** Detailed info about individual MISP event *****

@app.route('/misp_event/')
@app.route('/misp_event/<event_id>')
def misp_event(event_id=None):
    if not misp_inst:
        return render_template("misp_event.html", error="Cannot connect to MISP instance")
    if not event_id:
        return render_template("misp_event.html", error="MISP event id not specified")

    event = misp_inst.search(controller="events", eventid=int(event_id))

    if not event:
        return render_template('misp_event.html', error="Event does not exist in MISP instance")
    else:
        event = event[0]['Event']

        # find tlp tag, if tlp tag is not used, set green as default
        tlp = "green"
        for tag in event.get('Tag', []):
            if "tlp" in tag['name']:
                tlp = tag['name'][4:]
                break

        return render_template('misp_event.html', event=event, tlp=tlp)


# ***** Detailed info about individual AS *****

class SingleASForm(FlaskForm):
    asn = TextField('AS number', [validators.Regexp('^(AS)?\d+$', re.IGNORECASE,
            message='Must be a number, optionally preceded by "AS".')])


@app.route('/as/')
@app.route('/as/<asn>')
@app.route('/asn/')
@app.route('/asn/<asn>')
def asn(asn=None): # Can't be named "as" since it's a Python keyword
    form = SingleASForm(asn=asn)
    #print(asn,form.data)
    title = 'ASN detail search'
    if asn is None:
        # No ASN passed
        asinfo = {}
    elif form.validate():
        # Passed correct ASN
        asn = int(asn.lstrip("ASas")) # strip AS at the beginning
        if g.ac('assearch'):
            title = 'AS'+str(asn)
            rec = mongo.db.asn.find_one({'_id':asn})
        else:
            flash('Insufficient permissions to search/view ASNs.', 'error')
    else:
        # Wrong format of passed ASN
        asn = None
        rec = {}
    return render_template('asn.html', ctrydata=ctrydata, **locals())


# ***** Detailed info about individual IP block *****

# class SingleIPBlockForm(FlaskForm):
#     ip = TextField('IP block')#, [validators.IPAddress(message="Invalid IPv4 address")])

@app.route('/ipblock/')
@app.route('/ipblock/<ipblock>')
def ipblock(ipblock=None):
#     form = SingleIPForm(ip=ipaddr)
    #if form.validate():
    if not ipblock:
        return make_response("ERROR: No IP block given.")
    if g.ac('ipblocksearch'):
        title = ipblock
        rec = mongo.db.ipblock.find_one({'_id': ipblock})
        if rec is not None:
            cursor = mongo.db.ip.find({'ipblock': ipblock}, {'_id': 1})
            rec['ips'] = []
            if cursor is not None:
                for val in cursor:
                    rec['ips'].append(int2ipstr(val['_id']))
    else:
        flash('Insufficient permissions to search/view IP blocks.', 'error')
    return render_template('ipblock.html', ctrydata=ctrydata, **locals())


# ***** Detailed info about individual Organization *****

@app.route('/org/')
@app.route('/org/<org>')
def org(org=None):
    if not org:
        return make_response("ERROR: No Organization ID given.")
    if g.ac('orgsearch'):
        title = org
        rec = mongo.db.org.find_one({'_id': org})
        if rec is not None:
            rec['ipblocks'] = []
            rec['asns'] = []
            cursor = mongo.db.ipblock.find({'org': org}, {'_id': 1})
            for val in cursor:
                rec['ipblocks'].append(val['_id'])
            cursor = mongo.db.asn.find({'org': org}, {'_id': 1})
            for val in cursor:
                rec['asns'].append(val['_id'])
    else:
        flash('Insufficient permissions to search/view Organizations.', 'error')
    return render_template('org.html', ctrydata=ctrydata, **locals())


# ***** Detailed info about individual BGP prefix *****

# Note: Slash ('/') in the prefix must be replaced by underscore ('_') in URL, e.g.:
# "192.168.0.0/16" -> "192.168.0.0_16"
@app.route('/bgppref/')
@app.route('/bgppref/<bgppref>')
def bgppref(bgppref=None):
    if not bgppref:
        return make_response("ERROR: No BGP Prefix given.")
    bgppref = bgppref.replace('_','/')

    if g.ac('bgpprefsearch'):
        title = bgppref
        rec = mongo.db.bgppref.find_one({'_id': bgppref})
        if rec is not None:
            cursor = mongo.db.ip.find({'bgppref': bgppref}, {'_id': 1})
            rec['ips'] = []
            for val in cursor:
                rec['ips'].append(int2ipstr(val['_id']))
    else:
        flash('Insufficient permissions to search/view BGP prefixes.', 'error')
    return render_template('bgppref.html', ctrydata=ctrydata, **locals())

# ***** NERD status information *****

@app.route('/status')
def get_status():
    cnt_ip = mongo.db.ip.count()
    cnt_bgppref = mongo.db.bgppref.count()
    cnt_asn = mongo.db.asn.count()
    cnt_ipblock = mongo.db.ipblock.count()
    cnt_org = mongo.db.org.count()
    idea_queue_len = len(os.listdir(WARDEN_DROP_PATH))
    
    if "upd_cnt_file" in config:
        try:
            upd_cnt = open(config.get("upd_cnt_file")).read().split("\n")
            upd_processed = sum(map(int, upd_cnt[10:20]))
            upd_queue = int(upd_cnt[20])
        except Exception as e:
            upd_processed = "(error) " + str(e)
            upd_queue = 0
    else:
        upd_processed = "(N/A)"
        upd_queue = 0
    
    try:
        if "data_disk_path" in config:
            disk_usage = subprocess.check_output(["df", config.get("data_disk_path"), "-P"]).decode('ascii').splitlines()[1].split()[4]
        else:
            disk_usage = "(N/A)"
    except Exception as e:
        disk_usage = "(error) " + str(e)
    
    return jsonify(
        cnt_ip=cnt_ip,
        cnt_bgppref=cnt_bgppref,
        cnt_asn=cnt_asn,
        cnt_ipblock=cnt_ipblock,
        cnt_org=cnt_org,
        idea_queue=idea_queue_len,
        update_queue=upd_queue,
        updates_processed=upd_processed,
        disk_usage=disk_usage
    )


# ***** Plain-text list of IP addresses *****
# (gets the same parameters as /ips/)

@app.route('/iplist')
@app.route('/iplist/')
def iplist():

    form = IPFilterFormUnlimitedDef(request.args)
    
    if not g.user or not g.ac('ipsearch'):
        return Response('ERROR: Unauthorized', 403, mimetype='text/plain')
    
    if not form.validate():
        return Response('ERROR: Bad parameters: ' + '; '.join('{}: {}'.format(name, ', '.join(errs)) for name, errs in form.errors.items()), 400, mimetype='text/plain')
    
    sortby = sort_mapping[form.sortby.data]
    
    query = create_query(form)
    
    try:
        # Perform DB query
        results = mongo.db.ip.find(query, {'_id': 1}).limit(form.limit.data)
        if sortby != "none":
            results.sort(sortby, 1 if form.asc.data else -1)
        return Response(''.join(int2ipstr(res['_id'])+'\n' for res in results), 200, mimetype='text/plain')
    except pymongo.errors.ServerSelectionTimeoutError:
        return Response('ERROR: Database connection error', 503, mimetype='text/plain')


# ******************** Map ********************
@app.route('/map/')
def map_index():
    return render_template("map.html", **locals())

# ******************** Static/precomputed data ********************

FILE_IP_REP = "/data/web_data/ip_rep.csv"

@app.route('/data/')
def data_index():
    try:
        ip_rep_file_size = os.stat(FILE_IP_REP).st_size
    except OSError:
        ip_rep_file_size = None
    return render_template("data.html", **locals())

@app.route('/data/ip_rep.csv')
def data_ip_rep():
    try:
        return flask.send_file(FILE_IP_REP, mimetype="text/plain", as_attachment=True)
    except OSError:
        return Response('ERROR: File not found on the server', 500, mimetype='text/plain')




# ****************************** API ******************************

@app.route('/api/v1/user_info')
def api_user_info():
    """Return account information if user is successfully authenticated"""
    if not g.ac('ipsearch'):
        return API_RESPONSE_403
    data = {
        'userid': g.user.get('fullid'),
#        'name': g.user.get('name', ''),
#         'email': g.user.get('email', ''),
#         'org': g.user.get('org', ''),
        'groups': list(g.user.get('groups', [])),
        'rate-limit-bucket-size': g.user.get('rl-bs') or rate_limiter.def_bucket_size,
        'rate-limit-tokens-per-sec': g.user.get('rl-tps') or rate_limiter.def_tokens_per_sec,
    }
    return Response(json.dumps(data), 200, mimetype='application/json')


def get_ip_info(ipaddr, full):
    data = {
        'err_n' : 400,
        'error' : "No IP address specified",
        'ip' : ipaddr
    }

    if not ipaddr:
        return False, Response(json.dumps(data), 400, mimetype='application/json')

    form = SingleIPForm(ip=ipaddr)
    if not form.validate():
        data['error'] = "Bad IP address"
        return False, Response(json.dumps(data), 400, mimetype='application/json')

    ipint = ipstr2int(form.ip.data) # Convert string IP to int

    if full:
        ipinfo = mongo.db.ip.find_one({'_id':ipint})
    else:
        ipinfo = mongo.db.ip.find_one({'_id':ipint}, {'rep': 1, 'fmp': 1, 'hostname': 1, 'bgppref': 1, 'ipblock': 1, 'geo': 1, 'bl': 1, 'tags': 1})
    if not ipinfo:
        data['err_n'] = 404
        data['error'] = "IP address not found"
        return False, Response(json.dumps(data), 404, mimetype='application/json')

    ipinfo['_id'] = int2ipstr(ipinfo['_id']) # Convert int IP to string

    attach_whois_data(ipinfo, full)
    return True, ipinfo


def conv_dates(rec):
    """Convert datetimes in a record to YYYY-MM-DDTMM:HH:SS string"""
    for key in ('ts_added', 'ts_last_update'):
        if key in rec and isinstance(rec[key], datetime):
            rec[key] = rec[key].strftime("%Y-%m-%dT%H:%M:%S")


def attach_whois_data(ipinfo, full):
    if not full:
        # Only attach ASN number(s)
        if 'bgppref' in ipinfo:
            bgppref_rec = mongo.db.bgppref.find_one({'_id': ipinfo['bgppref']}, {'asn': 1})
            if bgppref_rec is None:
                print("ERROR: Can't find BGP prefix '{}' in database (trying to enrich IP {})".format(ipinfo['bgppref'], ipinfo['_id']))
                return
            if 'asn' in bgppref_rec:
                ipinfo['asn'] = bgppref_rec['asn']
        return
    
    # Full - attach full records of related BGP prefix, ASNs, IP block, Org
    # IP->BGPpref
    if 'bgppref' in ipinfo:
        bgppref_rec = clean_secret_data(mongo.db.bgppref.find_one({'_id':ipinfo['bgppref']}))
        if bgppref_rec is None:
            print("ERROR: Can't find BGP prefix '{}' in database (trying to enrich IP {})".format(ipinfo['bgppref'], ipinfo['_id']))
        else:
            # BGPpref->ASN(s)
            asn_list = []
            for asn in bgppref_rec['asn']:
                asn_rec = clean_secret_data(mongo.db.asn.find_one({'_id':asn}))
                if asn_rec is None:
                    print("ERROR: Can't find ASN '{}' in database (trying to enrich IP {}, bgppref {})".format(asn, ipinfo['_id'], bgppref_rec['_id']))
                else:
                    # ASN->Org
                    if 'org' in asn_rec:
                        org_rec = clean_secret_data(mongo.db.org.find_one({'_id':asn_rec['org']}))
                        if org_rec is None:
                            print("ERROR: Can't find Org '{}' in database (trying to enrich IP {}, bgppref {}, ASN {})".format(asn_rec['org'], ipinfo['_id'], bgppref_rec['_id'], asn))
                        else:
                            conv_dates(org_rec)
                            asn_rec['org'] = org_rec

                    del asn_rec['bgppref']
                    conv_dates(asn_rec)
                    asn_list.append(asn_rec)

            del bgppref_rec['asn']
            conv_dates(bgppref_rec)
            ipinfo['bgppref'] = bgppref_rec
            ipinfo['asn'] = asn_list

    # IP->ipblock
    if 'ipblock' in ipinfo:
        ipblock_rec = clean_secret_data(mongo.db.ipblock.find_one({'_id':ipinfo['ipblock']}))
        if ipblock_rec is None:
            print("ERROR: Can't find IP block '{}' in database (trying to enrich IP {})".format(ipinfo['ipblock'], ipinfo['_id']))
        else:
            # ipblock->org
            if "org" in ipblock_rec:
                org_rec = clean_secret_data(mongo.db.org.find_one({'_id':ipblock_rec['org']}))
                if org_rec is None:
                    print("ERROR: Can't find Org '{}' in database (trying to enrich IP {}, ipblock '{}')".format(ipblock_rec['org'], ipinfo['_id'], ipblock_rec['_id']))
                else:
                    conv_dates(org_rec)
                    ipblock_rec['org'] = org_rec

            conv_dates(ipblock_rec)
            ipinfo['ipblock'] = ipblock_rec


def clean_secret_data(data):
    """Remove all keys starting with '_' (except '_id') from dict."""
    if data is not None:
        for i in list(data):
            if i.startswith("_") and i != "_id":
                del data[i]
    return data


# ***** NERD API BasicInfo - helper funcs *****
def get_basic_info_dic(val):
    geo_d = {}
    if 'geo' in val.keys():
        geo_d['ctry'] = val['geo'].get('ctry', "unknown")

    bl_l = []
    for l in val.get('bl', []):
        bl_l.append(l['n']) # TODO: shouldn't there be a check for v=1?

    tags_l = []
    for l in val.get('tags', []):
        d = {
            'n' : l,
            'c' : val['tags'][l]['confidence']
        }

        tags_l.append(d)

    data = {
        'ip' : val['_id'],
        'rep' : val.get('rep', 0.0),
        'fmp' : val.get('fmp', {'general': 0.0}),
        'hostname' : (val.get('hostname', '') or '')[::-1],
        'ipblock' : val.get('ipblock', ''),
        'bgppref' : val.get('bgppref', ''),
        'asn' : val.get('asn',[]),
        'geo' : geo_d,
        'bl'  : bl_l,
        'tags'  : tags_l
    }

    return data

def get_basic_info_dic_short(val):
    # only 'rep' and 'tags' fields
    tags_l = []
    for l in val.get('tags', []):
        d = {
            'n' : l,
            'c' : val['tags'][l]['confidence']
        }
        tags_l.append(d)

    data = {
        'ip' : val['_id'],
        'rep' : val.get('rep', 0.0),
        'tags'  : tags_l
    }
    return data


# ***** NERD API BasicInfo *****
@app.route('/api/v1/ip/<ipaddr>')
def get_basic_info(ipaddr=None):
    if not g.ac('ipsearch'):
        return API_RESPONSE_403

    ret, val = get_ip_info(ipaddr, False)
    if not ret:
        return val

    binfo = get_basic_info_dic(val)

    return Response(json.dumps(binfo), 200, mimetype='application/json')


# ***** NERD API Reputation/FMP only *****

@app.route('/api/v1/ip/<ipaddr>/rep')
def get_ip_rep(ipaddr=None):
    if not g.ac('ipsearch'):
        return API_RESPONSE_403

    # Check validity of ipaddr
    try:
        ipaddress.IPv4Address(ipaddr)
    except ValueError:
        data = {'err_n': 400, 'error': 'Bad IP address'}
        return Response(json.dumps(data), 400, mimetype='application/json')

    ipint = ipstr2int(ipaddr)

    # Load 'rep' field of the IP from MongoDB
    ipinfo = mongo.db.ip.find_one({'_id': ipint}, {'rep': 1})
    if not ipinfo:
        data = {'err_n': 404, 'error': 'IP address not found', 'ip': ipaddr}
        return Response(json.dumps(data), 404, mimetype='application/json')

    # Return simple JSON
    data = {
        'ip': int2ipstr(ipinfo['_id']),
        'rep': ipinfo.get('rep', 0.0),
    }
    return Response(json.dumps(data), 200, mimetype='application/json')


@app.route('/api/v1/ip/<ipaddr>/fmp')
def get_ip_fmp(ipaddr=None):
    if not g.ac('ipsearch'):
        return API_RESPONSE_403

    # Check validity of ipaddr
    try:
        ipaddress.IPv4Address(ipaddr)
    except ValueError:
        data = {'err_n': 400, 'error': 'Bad IP address'}
        return Response(json.dumps(data), 400, mimetype='application/json')

    ipint = ipstr2int(ipaddr)

    # Load 'fmp' field of the IP from MongoDB
    ipinfo = mongo.db.ip.find_one({'_id': ipint}, {'fmp': 1})
    if not ipinfo:
        data = {'err_n': 404, 'error': 'IP address not found', 'ip': ipaddr}
        return Response(json.dumps(data), 404, mimetype='application/json')

    # Return simple JSON
    data = {
        'ip': int2ipstr(ipinfo['_id']),
        'fmp': ipinfo.get('fmp', {'general': 0.0}),
    }
    return Response(json.dumps(data), 200, mimetype='application/json')



@app.route('/api/v1/ip/<ipaddr>/test') # No query to database - for performance comparison
def get_ip_rep_test(ipaddr=None):
    #if not g.ac('ipsearch'):
    #    return API_RESPONSE_403

    # Return simple JSON
    data = {
        'ip': ipaddr,
        'rep': 0.0,
    }
    return Response(json.dumps(data), 200, mimetype='application/json')

# ***** NERD API FullInfo *****

@app.route('/api/v1/ip/<ipaddr>/full')
def get_full_info(ipaddr=None):
    if not g.ac('ipsearch'):
        return API_RESPONSE_403

    ret, val = get_ip_info(ipaddr, True)
    if not ret:
        return val

    data = {
        'ip' : val['_id'],
        'rep' : val.get('rep', 0.0),
        'fmp' : val.get('fmp', {'general': 0.0}),
        'hostname' : (val.get('hostname', '') or '')[::-1],
        'ipblock' : val.get('ipblock', ''),
        'bgppref' : val.get('bgppref', ''),
        'asn' : val.get('asn',[]),
        'geo' : val.get('geo', None),
        'ts_added' : val['ts_added'].strftime("%Y-%m-%dT%H:%M:%S"),
        'ts_last_update' : val['ts_last_update'].strftime("%Y-%m-%dT%H:%M:%S"),
        'last_activity' : val['last_activity'].strftime("%Y-%m-%dT%H:%M:%S") if 'last_activity' in val else None,
        'bl' : [ {
                'name': bl['n'],
                'last_check': bl['t'].strftime("%Y-%m-%dT%H:%M:%S"),
                'last_result': True if bl['v'] else False,
                'history': [t.strftime("%Y-%m-%dT%H:%M:%S") for t in bl['h']]
            } for bl in val.get('bl', []) ],
        'events' : val.get('events', []),
        'misp_events' : val.get('misp_events', []),
        'events_meta' : {
            'total': val.get('events_meta', {}).get('total', 0.0),
            'total1': val.get('events_meta', {}).get('total1', 0.0),
            'total7': val.get('events_meta', {}).get('total7', 0.0),
            'total30': val.get('events_meta', {}).get('total30', 0.0),
        },
    }

    return Response(json.dumps(data), 200, mimetype='application/json')

# ***** NERD API IPSearch *****

@app.route('/api/v1/search/ip/')
def ip_search(full = False):
    err = {}
    if not g.ac('ipsearch'):
        return API_RESPONSE_403

    # Get output format
    output = request.args.get('o', 'json')
    if output not in ('json', 'list', 'short'):
        err['err_n'] = 400
        err['error'] = 'Unrecognized value of output parameter: ' + output
        return Response(json.dumps(err), 400, mimetype='application/json')

    list_output = (output == "list")

    # Validate parameters
    if output == "list":
        form = IPFilterFormUnlimitedDef(request.args) # no limit when only asking for list of IPs
    elif g.ac('unlimited_search') and not full:
        form = IPFilterFormUnlimited(request.args) # possibility to specify no limit, but default is 20 as normal
    else:
        form = IPFilterForm(request.args) # otherwise limit must be between 1 and 1000 (TODO: allow more?)

    if not form.validate():
        err['err_n'] = 400
        err['error'] = 'Bad parameters: ' + '; '.join('{}: {}'.format(name, ', '.join(errs)) for name, errs in form.errors.items())
        return Response(json.dumps(err), 400, mimetype='application/json')

    # Perform DB query
    sortby = sort_mapping[form.sortby.data]
    query = create_query(form)
    
    if output == "list":
        outputfields =  {'_id': 1}
    elif output == "short": # short output format, only rep. score and tags are needed
        outputfields = {'_id': 1, 'rep': 1, 'tags': 1}
    elif output == "json": # normal output, get everything except 'events' (which are long and not needed)
        outputfields = {'events': 0}
    
    try:
        results = mongo.db.ip.find(query, outputfields).limit(form.limit.data)  # note: limit=0 means no limit
        if sortby != "none":
            results.sort(sortby, 1 if form.asc.data else -1)
        results = list(results)
    except pymongo.errors.ServerSelectionTimeoutError:
        err['err_n'] = 503
        err['error'] = 'Database connection error'
        return Response(json.dumps(err), 503, mimetype='application/json')

    # Return results
    if output == "list":
        return Response(''.join(int2ipstr(res['_id'])+'\n' for res in results), 200, mimetype='text/plain')

    # Convert _id from int to dotted-decimal string        
    for res in results:
        res['_id'] = int2ipstr(res['_id'])

    lres = []
    if output == "short":
        for res in results:
            lres.append(get_basic_info_dic_short(res))
    else:    
        for res in results:
            attach_whois_data(res, full)
            lres.append(get_basic_info_dic(res))

    return Response(json.dumps(lres), 200, mimetype='application/json')


# ***** Get summary info about IPs in given prefix *****
# Return:
#  - average reputation score of the prefix (sum of rep of present addresses divided by prefix size)
#  - number of IPs in the DB in the prefix
#  - list of the IPs

@app.route('/api/v1/prefix/<prefix>/<length>')
def prefix(prefix, length):
    err = {}
    if not g.ac('ipsearch'):
        return API_RESPONSE_403
    
    # Check parameters
    try:
        network = ipaddress.IPv4Network(prefix + '/' + length, strict=False)
    except ValueError:
        err['err_n'] = 400
        err['error'] = 'Bad parameters: invalid prefix'
        return Response(json.dumps(err), 400, mimetype='application/json')
    if network.prefixlen < 16:
        err['err_n'] = 400
        err['error'] = 'Bad parameters: the shortest supported prefix is /16'
        return Response(json.dumps(err), 400, mimetype='application/json')
    
    # Get list of all IPs from DB matching the prefix
    int_prefix_start = int(network.network_address)
    int_prefix_end = int(network.broadcast_address)
    query = {'$and': [{'_id': {'$gte': int_prefix_start}}, {'_id': {'$lt': int_prefix_end}}]}
    try:
        results = mongo.db.ip.find(query)
        results = list(results)
    except pymongo.errors.ServerSelectionTimeoutError:
        err['err_n'] = 503
        err['error'] = 'Database connection error'
        return Response(json.dumps(err), 503, mimetype='application/json')
    
    # Create a summary record
    sum_rep = 0.0
    ips = []
    for rec in results:
        sum_rep += rec.get('rep', 0.0)
        ips.append(int2ipstr(rec['_id']))

    result = {
        'rep': sum_rep / network.num_addresses,
        'num_ips': len(results),
        'ips': ips,
    }
    return Response(json.dumps(result), 200, mimetype='application/json')
    

# ***** NERD bad prefix list *****
# Return list of the worst BGP prefixes by their reputation score

# class BadPrefixForm(FlaskForm):
#     t = FloatField('Reputation score threshold', [validators.Optional(), validators.NumberRange(0, 1, 'Must be a number between 0 and 1')], default=0.01)
#     limit = IntegerField('Max number of results', [validators.Optional()], default=100)

@app.route('/api/v1/bad_prefixes')
def bad_prefixes():
    err = {}
    if not g.ac('ipsearch'):
        return API_RESPONSE_403

    # Parse parameters (threshold, limit)
#     form = BadPrefixForm(request.args)
#     if not form.validate():
#         err['err_n'] = 400
#         err['error'] = 'Bad parameters: ' + '; '.join('{}: {}'.format(name, ', '.join(errs)) for name, errs in form.errors.items())
#         return Response(json.dumps(err), 400, mimetype='application/json')
#     t = form.t.data
#     limit = form.limit.data
    try:
        t = float(request.args.get('t', 0.01))
        limit = int(request.args.get('limit', 100))
    except ValueError:
        err['err_n'] = 400
        err['error'] = 'Bad parameters'
        return Response(json.dumps(err), 400, mimetype='application/json')
    
    # Get the list of prefixes from database
    try:
        cursor = mongo.db.bgppref.find({"rep": {"$gt": t}}, {"rep": 1}).sort("rep", -1).limit(limit)
        results = list(cursor)
    except pymongo.errors.ServerSelectionTimeoutError:
        err['err_n'] = 503
        err['error'] = 'Database connection error'
        return Response(json.dumps(err), 503, mimetype='application/json')

    # Prepare output
    output = request.args.get('o', "json")
    if output == "json":
        res_list = [{'prefix': res['_id'], 'rep': res['rep']} for res in results]
        return Response(json.dumps(res_list), 200, mimetype='application/json')
    elif output == "text":
        return Response('\n'.join(res['_id']+'\t'+str(res['rep']) for res in results), 200, mimetype='text/plain')
    else:
        err['err_n'] = 400
        err['error'] = 'Unrecognized value of output parameter: ' + output
        return Response(json.dumps(err), 400, mimetype='application/json')


"""
***** NERD API Bulk IP Reputation *****

Endpoint for bulk IP address queries about the reputation score.
IP addresses can be passed either in binary format (4 bytes, big endian, no separator) or in a text format (ASCII, each IP separated with comma).
IP addresses are passed as raw data in POST form.
Format is selected by querying with header field "Content-Type: text/plain" for text format or "Content-Type: application/octet-stream" for binary format.

Returned data contain a list of reputation scores for each IP address queried in the same order IPs were passed to API. (text format)
Returned data contain an octet stream. Each 8 bytes represent a double precision data type. (binary format)
"""

@app.route('/api/v1/ip/bulk/', methods=['POST'])
def bulk_request():
    if not g.ac('ipsearch'):
        return API_RESPONSE_403

    ips = request.get_data()

    f = request.headers.get("Content-Type", "")
    if f == 'text/plain':
        ips = ips.decode("ascii")
        ip_list = [ipstr2int(ipstr) for ipstr in ips.split(',')]
    elif f == 'application/octet-stream':
        ip_list = []
        for x in range(0, int(len(ips) / 4)):
            addr, = struct.unpack('!I', ips[x * 4 : x * 4 + 4])
            ip_list.append(addr)
    else:
        return Response(json.dumps({'err_n': 400, 'error': 'Unsupported input data format: ' + f}), 400, mimetype='application/json')

    results = {el:0.0 for el in ip_list}

    res = mongo.db.ip.find({"_id": {"$in": ip_list}}, {"_id":1, "rep":1})
    if res:
        for ip in res:
            results[ip['_id']] = ip.get('rep', 0.0)

    if f == 'text/plain':
        return Response(''.join(['%s\n' % results[val] for val in ip_list]), 200, mimetype='text/plain')
    elif f == 'application/octet-stream':
        resp = bytearray()
        for x in ip_list:
            resp += struct.pack("d", results[x])
        return Response(resp, 200, mimetype='application/octet-stream')


# Custom error 404 handler for API
@app.errorhandler(404)
def page_not_found(e):
    if request.path.startswith("/api"):
        # API -> return error in JSON
        err = {
            'err_n': 404,
            'error': "Not Found - unrecognized API path",
        }
        return Response(json.dumps(err), 404, mimetype='application/json')
    else:
        # Otherwise return default error page
        return e


# ***** Passive DNS gateway *****
@app.route('/pdns/ip/<ipaddr>', methods=['GET'])
def pdns_ip(ipaddr=None):
    if not g.ac('pdns'):
        return API_RESPONSE_403
    url = config.get('pdns.url', None)
    token = config.get('pdns.token', None)
    if not url or not token:
        return Response(json.dumps({'status': 500, 'error': 'Passive DNS not configured'}), 500, mimetype='application/json')
    try:
        response = requests.get('{}ip/{}?token={}'.format(url, ipaddr, token))
    except requests.RequestException as e: # Connection error, just in case
        print(str(e), file=sys.stderr)
        return Response(json.dumps({'status': 502, 'error': 'Bad Gateway - cannot get information from PDNS server'}), 502, mimetype='application/json')
    if response.status_code == 200:
        return Response(json.dumps(response.json()), 200, mimetype='application/json')
    elif response.status_code == 404: # Return "not found" as success, just with empty list
        return Response("[]", 200, mimetype='application/json')
    else:
        return Response(json.dumps({'status': 502, 'error': 'Bad Gateway. Received response ({}): {}'.format(response.status_code, response.text)}), 502, mimetype='application/json')


# ***** Shodan gateway *****
@app.route('/api/shodan-info/<ipaddr>', methods=['GET'])
def get_shodan_response(ipaddr=None):
    if not g.ac('shodan'):
        return API_RESPONSE_403
    #print("(Shodan) got an incoming request {}".format(ipaddr))
    shodan_client = ShodanRpcClient()
    data = json.loads(shodan_client.call(ipaddr))
    return render_template('shodan_response.html', data=data)

# **********

if __name__ == "__main__":
    # Set global testing flag
    config.testing = True
    # Disable normal ways of logging in (since they doesn't work with built-in server)
    config['login']['methods'] = {}
    # Run built-in server
    app.run(host="127.0.0.1", debug=True)

