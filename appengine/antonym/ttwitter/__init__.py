from datetime import datetime
import logging
import random
import re
from urllib2 import HTTPError

# from oauth.oauth import OAuthToken
# from oauthtwitter import OAuthApi
import oauth2 as oauth
import twitter

from antonym import rrandom
from antonym.accessors import ConfigurationAccessor, TwitterResponseAccessor
from antonym.core import AppException, DataException, IllegalStateException
from antonym.mixer import Mixer
from antonym.model import TwitterResponse
from antonym.text.speakers import new_random_speaker
from antonym.text.speakers.core import SentenceSpeaker
from antonym.text.speakers.graph import NxGraphSpeaker
from antonym.text.tweeting import parse_tweet
from antonym.web import service_users
from antonym.web.activity import ActivityReporter

from katapult.core import Record
from katapult.numbers import safe_int


def user_hash(user):
    """ converts a twitter.User to a hash, including only properties I care about. """
    user_hash = { 'name': user.name, 
        'screen-name': user.screen_name, 
        'url': user.url,
        'statuses-count': user.statuses_count,
        'followers-count': user.followers_count,
        'friends-count': user.friends_count }
        
    if user.friends_count > 0:
        # converts to a string to avoid weird long decimals
        user_hash['follower-to-friend-ratio'] = str(round(float(user.followers_count) / float(user.friends_count), 2))
    
    return user_hash

def sorted_user_list(users):
    return sorted(users, key=lambda u: u['screen-name'])

def new_default_mixer():
    return Mixer(new_speaker())

def describe_status(status):
    return "%s: %s" % (status.user.screen_name, status.text)

def describe_status_with_timestamp(status):
    return "[%s] %s: %s" % (status.created_at, status.user.screen_name, status.text)


# general wrapper around Twitter exceptions
class TwitterException(AppException):
    pass


class ReadOnlyTwitterApi:

    __supported_methods = set([ "FetchPath", "GetDirectMessages", "GetFollowers", "GetFriends",
        "GetFriendsTimeline", "GetReplies", "GetUser", "GetUserTimeline" ])
    
    def __init__(self, delegate=None):
        self.__d = delegate or TwitterConnector.new_api()
        self.__reporter = ActivityReporter()
        self.__post_id = 1;

    def postUpdate(self, *args):
        msg = args[0]
        self.__post_id += 1;
        r = Record(id=self.__post_id)
        self.__decorate_status(r)
        return r
        
    def retweet(self, *args):
        tweet_id = args[0]
        r = Record(id=tweet_id)
        self.__decorate_status(r)
        return r
        
    def __getattr__(self, name):
        logging.debug("%s.%s" % (self.__class__.__name__, name))
        if name in self.__supported_methods and hasattr(self.__d, name):
            return getattr(self.__d, name)
        else:
           raise IllegalStateException("%s.%s not found" % (self.__class__.__name__, name)) 
    
    def __decorate_status(self, status):
        status.asDict = lambda: status.to_hash()
        status.user = Record()
        
    def __nonzero__(self):
        return True


class TwitterConnector:
    
    @classmethod
    def new_api(cls):
        config = ConfigurationAccessor.get_or_create()
        if config and config.twitter_oauth_enabled and config.twitter_access_token:
            logging.debug("creating OAuth API instance")
            api = cls._new_oauth_api(config.twitter_access_token)
        else:
            raise Exception("no OAuth token stored in config")
            
        if safe_int(config.twitter_read_only):
            logging.debug("creating read-only Twitter API")
            api = ReadOnlyTwitterApi(api)
        return api

    @classmethod
    def _new_basic_api(cls):
        # TODO: use memcache?
        u, p = service_users.twitter_default_creds()
        return twitter.Api(username=u, password=p, cache=None)

    @classmethod
    def get_request_token_and_auth_url(cls):
        key, secret = service_users.twitter_oauth_creds()
        api = OAuthApi(key, secret, cache=None)
        request_token = api.getRequestToken()
        return request_token, api.getAuthorizationURL(request_token)

    @classmethod
    def get_access_token(cls, request_token_key, request_token_secret):
        key, secret = service_users.twitter_oauth_creds()
        api = OAuthApi(key, secret, OAuthToken(request_token_key, request_token_secret), cache=None)
        return api.getAccessToken()

    @classmethod
    def _new_oauth_api(cls, encoded_access_token):
        consumer_key, consumer_secret = service_users.twitter_oauth_creds()
        access_token = oauth.Token.from_string(encoded_access_token)
        return twitter.Api(consumer_key=consumer_key, consumer_secret=consumer_secret,
            access_token_key=access_token.key, access_token_secret=access_token.secret,
            cache=TwitterMemcache())


class ActionSelector:
    
    # detailed actions
    NOTHING = "nothing"
    RESPONSE = "response"
    RANDOM_TWEET = "random-tweet"
    RETWEET = "retweet"
    TRENDING = "trending"
    
    # keys are UTC hour ranges, values are lists of tuples containing (action, weight) pairs
    # this is used to select what to do at a given time
    # do stuff from 7am-12am EST = 11am-4am UTC
    # relax from 12am-7am EST = 4am-11am UTC
    _active_weights = [(True, 2), (False, 8)]
    _passive_weights = [(True, 1), (False, 9)]
        
    # maps time of day to weights
    _times_to_weights = { xrange(0, 4): _active_weights, 
        xrange(4,11): _passive_weights,
        xrange(11,24): _active_weights }

    # weighted probabilities of actions to perform if there is nothing to respond to
    _actions_weighted = [(RANDOM_TWEET,6), (RETWEET,2), (TRENDING, 2)]

    def should_act(self):
        now = datetime.utcnow()
        # TODO: optimize range search
        logging.debug("time: %s" % now)
        should_act = False
        for hour_range, actions in self._times_to_weights.iteritems():
            if now.hour in hour_range:
                logging.debug("actions: %s" % actions)
                should_act = rrandom.select_weighted_with_replacement(actions)
                break
        return should_act

    def select_action(self):
        return rrandom.select_weighted_with_replacement(self._actions_weighted)


class TwitterActor(object):
    
    MASTER_USERNAME = "mhawthorne"
    
    def __init__(self, selector=None, twitter_api=None, reporter=None, analyzer=None):
        self.__selector = selector or ActionSelector()
        self.__twitter = twitter_api or TwitterConnector.new_api()
        self.__reporter = reporter or ActivityReporter()
        self.__analyzer = analyzer or TweetAnalyzer()

    def __select_speaker(self):
        speaker_name, speaker = new_random_speaker()
        logging.debug("selected speaker: %s" % speaker.__class__.__name__)
        return speaker
    
    def __mix(self):
        speaker = self.__select_speaker()
        sources, content = Mixer(speaker).mix_random_limit_sources(2, degrade=True)
        logging.debug("mixing random tweet: {%s} %s" % (";".join([s.name for s in sources]), content))
        return sources, content
        
    def latest_statuses(self, count=None):
        return self.__twitter.GetUserTimeline(count=count)
        
    def mix(self):
        """
        returns:
            twitter.Status
        """
        sources, content = self.__mix()
        self.__reporter.posted(content)
        return self.__twitter.PostUpdate(content)
    
    def mix_trend(self):
        # query trends
        trends = self.__twitter.GetTrendsDaily()
        hashtags = set()
        for group in trends:
            for trend in group:
                name = trend.name
                if name.startswith("#"):
                    hashtags.add(name)
        
        # select random trend
        # needs to pass a list here since sets can't be indexed
        logging.debug("trends: %s" % hashtags)
        trend = random.choice([h for h in hashtags])
        logging.debug("selected trend: %s" % trend)
        
        # mix
        sources, content = self.__mix()
        tweet_content = "%s %s" % (content, trend)
        logging.debug("tweet: %s" % tweet_content)
        self.__reporter.posted(tweet_content)
        return self.__twitter.PostUpdate(tweet_content)
        
    def messages(self, since=None):
        """
        returns:
            tuple of (direct message list, menetion list)
        """
        try:
            # I use list comprehensions since I suspect that these methods return iterators
            # reversing the lists puts them in chronological order
            directs = [m for m in self.__twitter.GetDirectMessages(since=since)]
            directs.reverse()
        
            mentions = [r for r in self.__twitter.GetReplies(since=since)]
            mentions.reverse()
        
            return directs, mentions
        except HTTPError, e:
            raise TwitterException(e)
            
    def respond(self):
        directs, mentions = self.messages()

        direct = None
        mention = None
        
        try:
            direct = self.__direct_response(directs)
        except AppException, e:
            logging.error(e)
        
        try:
            mention = self.__public_response(mentions)
        except AppException, e:
            logging.error(e)
        
        return direct, mention

    def retweet(self):
        statuses = self.__twitter.GetFriendsTimeline(count=10)

        retweet_source = None
        for candidate in statuses:
            logging.debug("candidate: %s" % candidate)
            
            # TODO: should query for type=retweet
            # otherwise I may retweet a mention
            
            # checks if status has been retweeted
            response = TwitterResponseAccessor.get_by_message_id(str(candidate.id))
            
            # if msg was already retweeted, removed from set and let loop continue
            if not response:
                logging.debug("not previously retweeted: %s" % candidate.id)
                retweet_source = candidate
            else:
                logging.debug("found retweet for message %s" % response.message_id)
                continue
                
            if self.__analyzer.should_retweet(candidate):
                retweet_source = candidate
                break
            else:
                logging.debug("not retweeting: %s" % describe_status(candidate))
            
        if not retweet_source:
            raise DataException("could not find anything to retweet")
            
        pretty_retweet = describe_status(retweet_source)
        logging.debug("retweeting '%s'" % pretty_retweet)
        retweet = self.__twitter.PostRetweet(retweet_source.id)
        # stores retweet
        TwitterResponseAccessor.create(str(retweet_source.id), response_id=str(retweet.id),
            tweet_type=TwitterResponse.RETWEET, user=retweet_source.user.screen_name)
        self.__reporter.retweeted(pretty_retweet)
        return retweet_source, pretty_retweet
        
    def act(self, force_act=False, action=None, skip_responses=False):
        """
        returns:
            (action, response) tuple.  response type depends on the action that was performed.
        """        
        if not force_act:
            config = ConfigurationAccessor.get_or_create()
            if config and (config.is_tweeting is not None) and (not safe_int(config.is_tweeting)):
                logging.debug("config.is_tweeting is False; hiding")
                return ()
        
        result = []
        responded = False
        if not skip_responses:
            try:
                direct, response = self.respond()
                if (direct or response):
                    # a response to a direct message or mention was generated
                    responded = True
                    if direct:
                        result.append(direct.AsDict())
                    if response:
                        result.append(response.AsDict())
            except Exception, e:
                logging.error(e)
                
        if not responded:
            # no response was generated
            should_act = force_act or self.__selector.should_act()
            logging.debug("should_act: %s" % should_act)

            result.append(should_act)
            if not should_act:
                self.__reporter.did_not_act()
            else:
                # uses action parameter or selects one
                if not action: action = self.__selector.select_action()
                result.append(action)
                logging.info("action: %s" % action)
                if action == ActionSelector.RETWEET:
                    # is there anything to retweet?
                    retweet, summary = self.retweet()
                    result.append(summary)
                elif action == ActionSelector.RANDOM_TWEET:
                    # no message; send random tweet
                    status = self.mix()
                    result.append(describe_status(status))
                elif action == ActionSelector.TRENDING:
                    status = self.mix_trend()
                    result.append(describe_status(status))
                else:
                    # logging.error("unexpected action '%s'" % action)
                    raise Exception("unexpected action '%s'" % action)
                    
        return tuple(result)
    

    def follow(self):
        self.__twitter.CreateFriendship()

    def message_master(self, message):
        self.__twitter.postDirectMessage(cls.MASTER_USERNAME, message)

    def __direct_response(self, directs):
        direct = None
        
        for d in directs:
            logging.info("found direct message %s [%s %s] %s" % (d.id, d.created_at, d.sender_screen_name, d.text))
            response = TwitterResponseAccessor.get_by_message_id(str(d.id))
            if not response:
                direct = d
                break
            else:
                logging.debug("found response to direct message %s" % d.id)
        
        sent_message = None
        if direct:
            speaker = self.__select_speaker()
            sources, response_text = Mixer(speaker).mix_response(direct.text, min_results=1)
            logging.info("responding to direct message %s %s" % (direct.id, response_text))
            sent_message = self.__twitter.PostDirectMessage(direct.sender_screen_name, response_text)
            self.__reporter.posted(response_text)
            TwitterResponseAccessor.create(str(direct.id), response_id=str(sent_message.id), user=direct.sender_screen_name) 

        return sent_message
        
    def __public_response(self, messages):
        message = None
        
        for m in messages:
            pretty_message = "%s [%s %s] %s" % (m.id, m.created_at, m.user.screen_name, m.text)
            logging.info("found public message: %s" % pretty_message)
            if not self.__analyzer.should_respond(m):
                logging.info("not responding")
                continue
                
            response = TwitterResponseAccessor.get_by_message_id(str(m.id))
            if not response:
                message = m
                break
            else:
                logging.debug("found response to public message %s" % m.id)
        
        sent_message = None
        if message:
            # TODO: search for username also
            username = message.user.screen_name
            parsed_tweet = parse_tweet(message.text)
            plain_tweet = parsed_tweet.plain_text
            speaker = self.__select_speaker()
            sources, mix = Mixer(speaker).mix_response(plain_tweet, min_results=1, max_length=130-len(username))
            response_text = "@%s %s" % (username, mix)
            logging.info("responding to public message %s: %s" % (message.id, response_text))
            
            sent_message = self.__twitter.PostUpdate(response_text, message.id)
            TwitterResponseAccessor.create(str(message.id), response_id=str(sent_message.id), user=username, tweet_type=TwitterResponse.MENTION) 
            self.__reporter.posted(response_text)

        return sent_message


class TwitterMemcache(object):
    """ TODO: implement this """
    
    def __init__(self, root_directory=None):
        pass

    def __log(self, msg):
        logging.debug("[%s] %s" % (self.__class__.__name__, msg))

    def Get(self,key):
        self.__log("get %s" % key)

    def Set(self,key,data):
        self.__log("set %s %s" % (key, data))
        pass

    def Remove(self,key):
        self.__log("remove %s" % key)
        pass

    def GetCachedTime(self,key):
        self.__log("getCachedTime %s" % key)
        pass


class TweetAnalyzer:
    
    NO_RESPONSE_REGEX = re.compile("^.*RT @livelock.*$")
    
    # TODO: move to config
    USER_BLACKLIST = set([ "mardsid", "livelock" ])
    
    def should_respond(self, message):
        """
        returns:
            True or False
        """
        return not self.NO_RESPONSE_REGEX.match(message.text)
    
    def should_retweet(self, message):
        """
        returns:
            True or False
        """
        result = True
        if message.user.screen_name in self.USER_BLACKLIST:
            result = False
        elif self.NO_RESPONSE_REGEX.match(message.text):
            result = False
        return result
