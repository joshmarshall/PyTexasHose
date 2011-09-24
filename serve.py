from tornado.web import RequestHandler, Application, asynchronous
from tornado.websocket import WebSocketHandler
from tornado.httpclient import AsyncHTTPClient
from tornado.iostream import IOStream
from tornado.ioloop import IOLoop
from tornado.escape import xhtml_escape
import json
import uuid
import socket
import time

CLIENTS = {} # just in memory for right now
IRC_CLIENTS = {}
CACHE = []

SETTINGS = {
    "port": 8888,
    "track": "pytexas",
    "max_cache": 50,

    # Twitter settings
    "username": "SET_IN_LOCAL_SETTINGS",
    "password": "SET_IN_LOCAL_SETTINGS",

    # Arg / cookie settings
    "client_id_cookie": "client_id",
    "last_time_argument": "last_time",

    #IRC settings
    "irc_host": "irc.freenode.net",
    "irc_port": 6667,
    "irc_channel": "pytexas",
}

try:
    from settings import SETTINGS as local_settings
    SETTINGS.update(local_settings)
except ImportError:
    # No local settings
    pass

class PageHandler(RequestHandler):
    """ Just a simple handler for loading the base index page. """

    def get(self):
        host = self.request.host
        if ":" not in host:
            host += ":%s" % SETTINGS["port"]
        self.render("index.htm", host=host)

class PollHandler(RequestHandler):

        @asynchronous
        def get(self):
            """ Long polling group """
            self.client_id = self.get_cookie(SETTINGS["client_id_cookie"])
            last_time = int(self.get_argument(SETTINGS["last_time_argument"], 0))
            if not self.client_id:
                self.client_id = uuid.uuid4().hex
                self.set_cookie(SETTINGS["client_id_cookie"], self.client_id)
            CLIENTS[self.client_id] = self
            for client_id, client in CLIENTS.copy().iteritems():
                if client_id == self.client_id:
                    continue
                try:
                    client.write_message({
                        "type": "clients",
                        "count": len(CLIENTS)
                    })
                except IOError:
                    # Invalid client -- closed or something
                    pass
            messages = {
                "type": "messages",
                "messages": [],
                "time": int(time.time())
            }
            for message in CACHE:
                # Show any new messages since they last requested
                if message["time"] > last_time:
                    messages["messages"].append(message)
            if messages["messages"]:
                "Yup, messages."
                messages["messages"].append(
                    ({"type": "clients", "count": len(CLIENTS)})
                )
                self.finish(messages)
            # else wait it out

        def write_message(self, message):
            """ Write a response and close connection """
            try:
                self.finish({
                    "type": "messages",
                    "messages":[message,],
                    "time": int(time.time())
                })
            except AssertionError:
                # we're already closed
                if self.client_id:
                    CLIENTS.pop(self.client_id)


class StreamHandler(WebSocketHandler):
    """ Watches the Twitter stream """

    client_id = None

    def open(self):
        """ Creates the client and watches stream """
        self.client_id = uuid.uuid4().hex
        CLIENTS[self.client_id] = self
        self.update_client_count()
        [self.write_message(message) for message in CACHE]

    def update_client_count(self):
        """ Broadcast updated client counts. """
        for client in CLIENTS.copy().values():
            try:
                client.write_message({
                    "type": "clients",
                    "count": len(CLIENTS)
                })
            except IOError:
                # Invalid client -- closed or something
                pass

    def on_message(self, message):
        """ Just a heartbeat, no real purpose """
        pass

    def on_close(self):
        """ Removes a client from the connection list """
        CLIENTS.pop(self.client_id)
        print "Client %s removed." % self.client_id
        self.update_client_count()


class TwitterStream(object):
    """ Twitter stream connection """

    _instance = None

    def __init__(self):
        """ Just set up the cache list and get first set """
        # prepopulating cache
        client = AsyncHTTPClient()
        client.fetch("http://search.twitter.com/search.json?q="+
            SETTINGS["track"], self.cache_callback)

    def cache_callback(self, response):
        """ Set up last fifty messages """
        messages = json.loads(response.body)["results"][:50]
        messages.reverse()
        for message in messages:
            try:
                text = message["text"]
                name = ""
                username = message["from_user"]
                avatar = message["profile_image_url"]
                CACHE.append({
                    "type": "tweet",
                    "text": text,
                    "name": name,
                    "username": username,
                    "avatar": avatar,
                    "time": 1
                })
            except KeyError:
                print "invalid", message
                continue
        self.open_twitter_stream()


    @classmethod
    def instance(cls):
        """ Returns the singleton """
        if not cls._instance:
            cls._instance = cls()
        return cls._instance

    def open_twitter_stream(self):
        """ Creates the client and watches stream """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        self.twitter_stream = IOStream(sock)
        self.twitter_stream.connect(("stream.twitter.com", 80))
        import base64
        base64string = base64.encodestring("%s:%s" % (SETTINGS["username"],
            SETTINGS["password"]))[:-1]
        headers = {"Authorization": "Basic %s" % base64string,
                   "Host": "stream.twitter.com"}
        request = ["GET /1/statuses/filter.json?track=%s HTTP/1.1" %
            SETTINGS["track"]]
        for key, value in headers.iteritems():
            request.append("%s: %s" % (key, value))
        request = "\r\n".join(request) + "\r\n\r\n"
        self.twitter_stream.write(request)
        self.twitter_stream.read_until("\r\n\r\n", self.on_headers)

    def on_headers(self, response):
        """ Starts monitoring for results. """
        status_line = response.splitlines()[0]
        response_code = status_line.replace("HTTP/1.1", "")
        response_code = int(response_code.split()[0].strip())
        if response_code != 200:
            raise Exception("Twitter could not connect: %s" % status_line)
        self.wait_for_message()

    def wait_for_message(self):
        """ Throw a read event on the stack. """
        self.twitter_stream.read_until("\r\n", self.on_result)

    def on_result(self, response):
        """ Gets length of next message and reads it """
        if (response.strip() == ""):
            return self.wait_for_message()
        length = int(response.strip(), 16)
        self.twitter_stream.read_bytes(length, self.parse_json)

    def parse_json(self, response):
        """ Checks JSON message """
        if not response.strip():
            # Empty line, happens sometimes for keep alive
            return self.wait_for_message()
        try:
            response = json.loads(response)
        except ValueError:
            print "Invalid response:"
            print response
            return self.wait_for_message()

        self.parse_response(response)

    def parse_response(self, response):
        """ Parse the twitter message """
        try:
            text = response["text"]
            name = response["user"]["name"]
            username = response["user"]["screen_name"]
            avatar = response["user"]["profile_image_url_https"]
        except KeyError, exc:
            print "Invalid tweet structure, missing %s" % exc
            return self.wait_for_message()

        message = {
            "type": "tweet",
            "text": text,
            "avatar": avatar,
            "name": name,
            "username": username,
            "time": int(time.time())
        }

        broadcast_message(message)
        self.wait_for_message()

def broadcast_message(message):
    """ Send message to all connected clients. """
    CACHE.append(message)
    while len(CACHE) > SETTINGS["max_cache"]:
        CACHE.pop(0)
    for client_id, client in CLIENTS.copy().iteritems():
        try:
            client.write_message(message)
        except IOError:
            print "Bad connection? "+client_id
            CLIENTS.pop(client_id)

class IRCStream(object):

    _instance = None

    @classmethod
    def instance(cls):
        """ Returns the singleton """
        if not cls._instance:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        self.stream = IOStream(sock)
        self.host = SETTINGS["irc_host"]
        self.channel = SETTINGS["irc_channel"]
        self.stream.connect((self.host, SETTINGS["irc_port"]))
        self.nick = "PyTexasBot"
        self.ident = "pytexasbot"
        self.real_name = "PyTexas StreamBot"

        self.stream.write("NICK %s\r\n" % self.nick)
        self.stream.write("USER %s %s blah :%s\r\n" % (self.ident, self.host,
            self.real_name))
        self.stream.write("JOIN #"+self.channel+"\r\n")
        self.monitor_output()

    def monitor_output(self):
        self.stream.read_until("\r\n", self.parse_line)

    def parse_line(self, response):
        response = response.strip()
        if response.startswith("PING "):
            request = response.replace("PING ", "")
            self.stream.write("PONG %s\r\n" % request)
        splitter = "PRIVMSG #%s :" % self.channel
        if splitter in response:
            parts = response.split(splitter)
            text = parts[1]
            if not text:
                # not going to throw out empty messages
                return self.monitor_output()
            nick = parts[0][1:].split("!")[0].strip()
            message = {
                "time": int(time.time()),
                "text": xhtml_escape(text),
                "name": nick,
                "username": "irc.freenode.net#pytexas",
                "type": "tweet",
                "avatar": None
            }
            broadcast_message(message)
        if response.startswith("ERROR"):
            raise Exception(response)
        else:
            print response
        self.monitor_output()


def main():
    """ Start the application and Twitter stream monitor """
    app = Application([
        (r"/", PageHandler),
        (r"/stream", StreamHandler),
        (r"/poll", PollHandler)
    ], static_path="static", debug=True)
    app.listen(SETTINGS["port"])
    ioloop = IOLoop.instance()
    # Monitor Twitter Stream once for all clients
    ioloop.add_callback(TwitterStream.instance)
    #ioloop.add_callback(IRCStream.instance)
    ioloop.start()


if __name__ == "__main__":
    main()
