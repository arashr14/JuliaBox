#! /usr/bin/env python

from jbox_util import log_info, esc_sessname, read_config, make_sure_path_exists, unquote, CloudHelper
from jbox_user import JBoxUser
from jbox_accounting import JBoxAccounting
from jbox_container import JBoxContainer
from jbox_crypto import signstr

import tornado.ioloop, tornado.web, tornado.auth
import base64, json, os.path, random, string
import docker

import datetime, traceback, isodate, pytz, httplib2
from oauth2client import GOOGLE_REVOKE_URI, GOOGLE_TOKEN_URI
from oauth2client.client import OAuth2Credentials, _extract_id_token


def rendertpl(rqst, tpl, **kwargs):
    #log_info('rendering template: ' + tpl)
    rqst.render("../www/" + tpl, **kwargs)

def is_valid_req(req):
    sessname = req.get_cookie("sessname")
    if None == sessname:
        return False
    sessname = sessname.replace('"', '')
    hostshell = req.get_cookie("hostshell").replace('"', '')
    hostupl = req.get_cookie("hostupload").replace('"', '')
    hostipnb = req.get_cookie("hostipnb").replace('"', '')
    signval = req.get_cookie("sign").replace('"', '')
    
    sign = signstr(sessname + hostshell + hostupl + hostipnb, cfg["sesskey"])
    if (sign != signval):
        log_info('not valid req. signature not matching')
        return False
    if not JBoxContainer.is_valid_container("/" + sessname, (hostshell, hostupl, hostipnb)):
        log_info('not valid req. container deleted or ports not matching')
        return False
    return True


class MainHandler(tornado.web.RequestHandler):
    def get(self):
        jbox_cookie = AuthHandler.get_session_cookie(self)
        if None == jbox_cookie:
            rendertpl(self, "index.tpl", cfg=cfg, err='')
        else:
            user_id = jbox_cookie['u']
            sessname = esc_sessname(user_id)
            
            if cfg["gauth"]:
                try:
                    jbuser = JBoxUser(user_id)
                except:
                    # stale cookie. we don't have the user in our database anymore
                    log_info("stale cookie. we don't have the user in our database anymore. user: " + user_id)
                    self.redirect('/hostlaunchipnb/')
                    return
                
                creds = jbuser.get_gtok()
                if creds != None:
                    try:
                        creds_json = json.loads(base64.b64decode(creds))
                        creds_json = self.renew_creds(creds_json)
                        authtok = creds_json['access_token']
                    except:
                        log_info("stale stored creds. will renew on next use. user: " + user_id)
                        creds = None
                        authtok = None
                else:
                    authtok = None
            else:
                creds = None
                authtok = None
            
            self.chk_and_launch_docker(sessname, creds, authtok, user_id)
            

    def chk_and_launch_docker(self, sessname, creds, authtok, user_id):
        cont = JBoxContainer.get_by_name(sessname)
        
        # TODO: check if container is not running and is backed up
        if ((None == cont) or (not cont.is_running())) and (JBoxContainer.num_active() >= cfg['numlocalmax']):
            nhops = self.get_argument('hop', 0)
            if nhops > cfg['numhopmax']:
                rendertpl(self, "index.tpl", cfg=cfg, err="Maximum number of JuliaBox instances active. Please try after sometime.")
            else:
                self.redirect('/?hop=' + str(nhops+1))
        else:
            cont = JBoxContainer.launch_by_name(sessname, True)
            (shellport, uplport, ipnbport) = cont.get_host_ports()
            sign = signstr(sessname + str(shellport) + str(uplport) + str(ipnbport), cfg["sesskey"])
            self.set_cookie("sessname", sessname)
            self.set_cookie("hostshell", str(shellport))
            self.set_cookie("hostupload", str(uplport))
            self.set_cookie("hostipnb", str(ipnbport))
            self.set_cookie("sign", sign)

            rendertpl(self, "ipnbsess.tpl", sessname=sessname, cfg=cfg, creds=creds, authtok=authtok, user_id=user_id)

    def renew_creds(self, creds):
        creds = OAuth2Credentials.from_json(json.dumps(creds))
        http = httplib2.Http(disable_ssl_certificate_validation=True) # pass cacerts otherwise
        creds.refresh(http)
        creds = json.loads(creds.to_json())
        return creds
    

class AuthHandler(tornado.web.RequestHandler, tornado.auth.GoogleOAuth2Mixin):
    AUTH_COOKIE = 'juliabox'
    AUTH_VALID_DAYS = 30
    AUTH_VALID_SECS = (AUTH_VALID_DAYS * 24 * 60 * 60)
    CRED_STORE = {}
    @tornado.web.asynchronous
    @tornado.gen.coroutine
    def get(self):
        if cfg["gauth"]:
            #self_redirect_uri should be similar to  'http://<host>/hostlaunchipnb/'
            self_redirect_uri = self.request.full_url()
            idx = self_redirect_uri.index("hostlaunchipnb/")
            self_redirect_uri = self_redirect_uri[0:(idx + len("hostlaunchipnb/"))]
            
            # state indicates the stage of auth during multistate auth
            state = self.get_argument('state', None)
                
            code = self.get_argument('code', False)            
            if code != False:
                user = yield self.get_authenticated_user(redirect_uri=self_redirect_uri, code=code)

                # get user info
                http = tornado.httpclient.AsyncHTTPClient()
                auth_string = "%s %s" % (user['token_type'], user['access_token'])
                response = yield http.fetch('https://www.googleapis.com/userinfo/v2/me', headers={"Authorization": auth_string})
                user_info = json.loads(response.body)

                user_id = user_info['email']
                sessname = esc_sessname(user_id)

                jbuser = JBoxUser(user_id, create=True)
                if state == 'store_creds':
                    creds = self.make_credentials(user)
                    jbuser.set_gtok(base64.b64encode(creds.to_json()))
                    jbuser.save()
                    #log_info(str(user))
                    #log_info(creds.to_json())
                else:
                    self.set_session_cookie(user_id)
                    if jbuser.is_new:
                        jbuser.save()
                self.redirect('/')
            else:
                if state == 'ask_gdrive':
                    jbox_cookie = AuthHandler.get_session_cookie(self)
                    scope = ['https://www.googleapis.com/auth/drive']
                    extra_params={'approval_prompt': 'force', 'access_type': 'offline', 'login_hint': jbox_cookie['u'], 'include_granted_scopes': 'true', 'state': 'store_creds'}
                else:
                    scope = ['profile', 'email']
                    extra_params={'approval_prompt': 'auto'}
                
                yield self.authorize_redirect(redirect_uri=self_redirect_uri,
                                client_id=self.settings['google_oauth']['key'],
                                scope=scope,
                                response_type='code',
                                extra_params=extra_params)
        else:
            sessname = unquote(self.get_argument("sessname"))
            self.set_session_cookie(sessname)
            self.redirect('/')
        

    def make_credentials(self, user):
        #return AccessTokenCredentials(user['access_token'], "juliabox")
        token_expiry = datetime.datetime.utcnow() + datetime.timedelta(seconds=int(user['expires_in']))
        id_token = _extract_id_token(user['id_token'])
        credential = OAuth2Credentials(
            access_token = user['access_token'],
            client_id = self.settings['google_oauth']['key'],
            client_secret = self.settings['google_oauth']['secret'],
            refresh_token = user['refresh_token'],
            token_expiry = token_expiry,
            token_uri = GOOGLE_TOKEN_URI,
            user_agent = None,
            revoke_uri = GOOGLE_REVOKE_URI,
            id_token = id_token,
            token_response = user)
        return credential

    def set_session_cookie(self, user_id):
        t = datetime.datetime.now(pytz.utc).isoformat()
        sign = signstr(user_id + t, cfg['sesskey'])
        
        jbox_cookie = { 'u': user_id, 't': t, 'x': sign }
        self.set_cookie(AuthHandler.AUTH_COOKIE, base64.b64encode(json.dumps(jbox_cookie)))
 
    @staticmethod
    def get_session_cookie(req):
        try:
            jbox_cookie = req.get_cookie(AuthHandler.AUTH_COOKIE)
            if jbox_cookie == None:
                return None
            jbox_cookie = json.loads(base64.b64decode(jbox_cookie))
            sign = signstr(jbox_cookie['u'] + jbox_cookie['t'], cfg['sesskey'])
            if sign != jbox_cookie['x']:
                log_info("signature mismatch for " + jbox_cookie['u'])
                return None
    
            d = isodate.parse_datetime(jbox_cookie['t'])
            age = (datetime.datetime.now(pytz.utc) - d).total_seconds()
            if age > AuthHandler.AUTH_VALID_SECS:
                log_info("cookie older than allowed days: " + jbox_cookie['t'])
                return None
            return jbox_cookie
        except:
            log_info("exception while reading cookie")
            traceback.print_exc()
            return None
 
    @staticmethod
    def fetch_auth_results(req):
        try:
            jbox_cookie = req.get_cookie(AuthHandler.AUTH_COOKIE)
            if jbox_cookie == None:
                return None
            jbox_cookie = json.loads(base64.b64decode(jbox_cookie))
            sign = signstr(jbox_cookie['s'] + jbox_cookie['t'], cfg['sesskey'])
            if sign != jbox_cookie['x']:
                log_info("signature mismatch for " + jbox_cookie['s'])
    
            d = isodate.parse_datetime(jbox_cookie['t'])
            age = (datetime.datetime.now(pytz.utc) - d).total_seconds()
            if age > AuthHandler.AUTH_VALID_SECS:
                log_info("cookie older than allowed days: " + jbox_cookie['t'])
                return None
            
            jbox_cookie['creds'] = AuthHandler.CRED_STORE[jbox_cookie['s']].to_json()
            return jbox_cookie
        except:
            log_info("exception while converting cookie to auth results")
            traceback.print_exc()
            return None


class AdminHandler(tornado.web.RequestHandler):
    def get(self):
        sessname = unquote(self.get_cookie("sessname"))
        jbox_cookie = AuthHandler.get_session_cookie(self)
        user_id = jbox_cookie['u']

        if len(sessname) == 0:
            self.send_error()

        cont = JBoxContainer.get_by_name(sessname)

        juliaboxver, upgrade_available = self.get_upgrade_available(cont)
        if self.do_upgrade(cont, upgrade_available):
            response = {'code': 0, 'data': ''}
            self.write(response)
            return

        admin_user = (sessname in cfg["admin_sessnames"]) or (cfg["admin_sessnames"] == [])

        sections = []
        loads = []
        d = {
                "admin_user" : admin_user,
                "sessname" : sessname, 
                "user_id" : user_id, 
                "created" : cont.time_created(), 
                "started" : cont.time_started(),
                "allowed_till" : (cont.time_started() + datetime.timedelta(seconds=cfg['expire'])),
                "mem" : cont.get_memory_allocated(), 
                "cpu" : cont.get_cpu_allocated(),
                "expire" : cfg['expire'],
                "sections" : sections,
                "loads" : loads,
                "juliaboxver" : juliaboxver,
                "upgrade_available" : upgrade_available
            }

        if admin_user:
            self.do_admin(sections, loads)

        rendertpl(self, "ipnbadmin.tpl", d=d, cfg=cfg)
    
    def do_upgrade(self, cont, upgrade_available):
        upgrade_id = self.get_argument("upgrade_id", '')
        if (upgrade_id == 'me') and (upgrade_available != None):
            cont.stop()
            cont.backup()
            cont.delete()
            return True
        return False

    def get_upgrade_available(self, cont):
        cont_images = cont.get_image_names()
        juliaboxver = cont_images[0]
        if (JBoxContainer.DCKR_IMAGE in cont_images) or ((JBoxContainer.DCKR_IMAGE + ':latest') in cont_images):
            upgrade_available = None
        else:
            upgrade_available = JBoxContainer.DCKR_IMAGE
            if ':' not in upgrade_available:
                upgrade_available = upgrade_available + ':latest'
        return (juliaboxver, upgrade_available)
        
    def do_admin(self, sections, loads):
        iac = []
        ac = []
        sections.append(["Active", ac])
        sections.append(["Inactive", iac])
        
        delete_id = self.get_argument("delete_id", '')
        stop_id = self.get_argument("stop_id", '')
        stop_all = (self.get_argument('stop_all', None) != None)
        
        if stop_all:
            all_containers = JBoxContainer.DCKR.containers(all=False)
            for c in all_containers:
                cont = JBoxContainer(c['Id'])
                cname = cont.get_name()

                if None == cname:
                    log_info("Admin: Not stopping unknown " + cont.debug_str())
                elif cname not in cfg["protected_docknames"]:
                    cont.stop()
                        
        elif not (stop_id == ''):
            cont = JBoxContainer(stop_id)
            cont.stop()
        elif not (delete_id == ''):
            cont = JBoxContainer(delete_id)
            cont.delete()

        # get them all again (in case we deleted some)
        jsonobj = JBoxContainer.DCKR.containers(all=all)
        for c in jsonobj:
            o = {}
            o["Id"] = c["Id"][0:12]
            o["Status"] = c["Status"]
            if ("Names" in c) and (c["Names"] != None): 
                o["Name"] = c["Names"][0]
            else:
                o["Name"] = "/None"
            
            if (c["Ports"] == None) or (c["Ports"] == []):
                iac.append(o)
            else:
                ac.append(o)
        
        # get cluster loads
        average_load = CloudHelper.get_cluster_average_stats('Load')
        if None != average_load:
            loads.append({'instance': 'Average', 'load': average_load})
            
        machine_loads = CloudHelper.get_cluster_stats('Load')
        if None != machine_loads:
            for n,v in machine_loads.iteritems():
                loads.append({'instance': n, 'load': v})

class PingHandler(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    @tornado.gen.coroutine
    def get(self):
        sessname = str(self.get_cookie("sessname")).replace('"', '')
        if is_valid_req(self):
            JBoxContainer.record_ping("/" + esc_sessname(sessname))
            self.set_status(status_code=204)
            self.finish()
        else:
            log_info("Invalid ping request for " + sessname)
            self.send_error(status_code=403)

def do_housekeeping():
    server_delete_timeout = cfg['expire'];
    JBoxContainer.maintain(delete_timeout=server_delete_timeout, delete_stopped_timeout=cfg['delete_stopped_timeout'], stop_timeout=cfg['inactivity_timeout'], protected_names=cfg['protected_docknames'])

def do_backups():
    JBoxContainer.backup_all()

    

if __name__ == "__main__":
    dckr = docker.Client()
    cfg = read_config()
    
    cloud_cfg = cfg['cloud_host']
    CloudHelper.configure(has_s3=cloud_cfg['s3'], has_dynamodb=cloud_cfg['dynamodb'], has_cloudwatch=cloud_cfg['cloudwatch'], region=cloud_cfg['region'], install_id=cloud_cfg['install_id'])
    
    backup_location = os.path.expanduser(cfg['backup_location'])
    backup_bucket = cloud_cfg['backup_bucket']
    make_sure_path_exists(backup_location)
    JBoxContainer.configure(dckr, cfg['docker_image'], cfg['mem_limit'], cfg['cpu_limit'], [os.path.join(backup_location, '${CNAME}')], backup_location, cfg['numlocalmax'], backup_bucket=backup_bucket)
    
    JBoxUser._init(table_name=cloud_cfg.get('jbox_users', 'jbox_users'), enckey=cfg['sesskey'])
    JBoxAccounting._init(table_name=cloud_cfg.get('jbox_accounting', 'jbox_accounting'))
    
    application = tornado.web.Application([
        (r"/", MainHandler),
        (r"/hostlaunchipnb/", AuthHandler),
        (r"/hostadmin/", AdminHandler),
        (r"/ping/", PingHandler)
    ])
    application.settings["cookie_secret"] = ''.join(random.choice(string.ascii_uppercase + string.digits) for x in xrange(32))
    application.settings["google_oauth"] = cfg["google_oauth"]
    application.listen(cfg["port"])
    
    ioloop = tornado.ioloop.IOLoop.instance()

    # run container maintainence every 10 minutes
    run_interval = 10*60*1000
    log_info("Container maintenance every " + str(run_interval/(60*1000)) + " minutes")
    ct = tornado.ioloop.PeriodicCallback(do_housekeeping, run_interval, ioloop)
    ct.start()

    # backup user files every 1 hour
    # check: configured expiry time must be at least twice greater than this
    run_interval = int(cfg['delete_stopped_timeout'])*1000/2
    if run_interval > 0:
        run_interval = min(run_interval, 60*60*1000)
    else:
        run_interval = 60*60*1000
    log_info("Container backups every " + str(run_interval/(60*1000)) + " minutes")
    log_info("Stopped containers would be deleted after " + str(int(cfg['delete_stopped_timeout'])/60) + " minutes")
    cbackup = tornado.ioloop.PeriodicCallback(do_backups, run_interval, ioloop)
    cbackup.start()
    
    ioloop.start()

