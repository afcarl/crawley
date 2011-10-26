from eventlet import GreenPool

from re import compile as re_compile
from urllib2 import urlparse

from crawley.config import CRAWLEY_ROOT_DIR
from crawley.http.managers import RequestManager
from crawley.http.cookies import CookieHandler
from crawley.http.response import Response
from crawley.extractors import XPathExtractor
from crawley.exceptions import AuthenticationError
from crawley.utils import url_matcher

user_crawlers = []

class CrawlerMeta(type):
    """
        This metaclass adds the user's crawlers to a list
        used by the CLI commands.
        Abstract base crawlers won't be added.
    """

    def __init__(cls, name, bases, dct):

        if not hasattr(cls, '__module__' ) or not cls.__module__.startswith(CRAWLEY_ROOT_DIR):
            user_crawlers.append(cls)
        super(CrawlerMeta, cls).__init__(name, bases, dct)


class BaseCrawler(object):
    """
        User's Crawlers must inherit from this class, may
        override some methods and define the start_urls list,
        the scrapers and the max crawling depth.
    """

    __metaclass__ = CrawlerMeta

    start_urls = []
    """ A list containing the start urls for the crawler"""

    allowed_urls = []
    """ A list of urls allowed for crawl"""

    scrapers = []
    """ A list of scrapers classes"""

    max_depth = -1
    """ The maximun crawling recursive level"""

    extractor = None
    """ The extractor class. Default is XPathExtractor"""

    post_urls = []
    """ The Post data for the urls. A List of tuples containing (url, data_dict)
        Example: ("http://www.mypage.com/post_url", {'page' : '1', 'color' : 'blue'})
    """

    login = None
    """ The login data. A tuple of (url, login_dict).
        Example: ("http://www.mypage.com/login", {'user' : 'myuser', 'pass', 'mypassword'})
    """

    _url_regex = re_compile(r'\b(([\w-]+://?|www[.])[^\s()<>]+(?:\([\w\d]+\)|([^[:punct:]\s]|/)))')

    def __init__(self, storage=None, sessions=None, debug=False):
        """
            Initializes the crawler

            params:

                storages: A list of entities

                sessions: Database or Documents persistant sessions

                debug: indicates if the crawler logs to stdout debug info
        """

        self.storage = storage

        if sessions is None:
            sessions = []

        self.sessions = sessions
        self.debug = debug

        if self.extractor is None:
            self.extractor = XPathExtractor

        self.extractor = self.extractor()
        self.cookie_hanlder = CookieHandler()

        self.pool = GreenPool()
        self.request_manager = RequestManager()
        
        self._initialize_scrapers()
        
    def _initialize_scrapers(self):
        """
            Instanciates all the scraper classes
        """
        
        self.scrapers = [scraper_class(debug=self.debug) for scraper_class in self.scrapers]            

    def _get_response(self, url, data=None):
        """
            Returns the response object from a request

            params:
                data: if this param is present it makes a POST.
        """
        data = self.request_manager.make_request(url, self.cookie_hanlder, data)
        response = Response(data, self.extractor.get_object(data), url)

        return response

    def _get_data(self, url, data=None):
        """
            Returns the response data from a request

            params:
                data: if this param is present it makes a POST.
        """

        for pattern, post_data in self.post_urls:
            if url_matcher(url, pattern):
                data = post_data

        return self._get_response(url, data)

    def _manage_scrapers(self, response):
        """
            Checks if some scraper is suited for data extraction on the current url.
            If so, gets the extractor object and delegate the scraping task
            to the scraper Object
        """
        scraped_urls = []

        for scraper in self.scrapers:

            urls = scraper.try_scrape(response)
                
            if urls is not None:
                
                self._commit()
                scraped_urls.extend(urls)

        return scraped_urls

    def _save_urls(self, url, new_url):
        """
            Stores the url in an [UrlEntity] Object
        """

        if self.storage is not None:

            self.storage(parent=url, href=new_url)
            self._commit()

    def _commit(self):
        """
            Makes a Commit in all sessions
        """

        for session in self.sessions:
            session.commit()

    def _validate_url(self, url):
        """
            Validates if the url is in the crawler's [allowed_urls] list.
        """

        if not self.allowed_urls:
            return True
        
        for pattern in self.allowed_urls:
            if url_matcher(url, pattern):
                return True
                
        return False

    def _fetch(self, url, depth_level=0):
        """
            Recursive url fetching.

            Params:
                depth_level: The maximun recursion level
                url: The url to start crawling
        """

        if not self._validate_url(url):
            return

        if self.debug:
            print "crawling -> %s" % url

        response = self._get_data(url)
        if response.raw_html is None:
            return
                
        urls = self._manage_scrapers(response)
        if not urls:
            urls = self.get_urls(response)
        
        for new_url in urls:
            self._save_urls(url, new_url)

            if depth_level >= self.max_depth and self.max_depth != -1:
                return

            self.pool.spawn_n(self._fetch, new_url, depth_level + 1)

    def _login(self):
        """
            If target pages are hidden behind a login then
            pass through it first.

            self.login can be None or a tuple containing
            (login_url, params_dict)
        """
        if self.login is None:
            return

        url, data = self.login
        if self._get_response(url, data) is None:
            raise AuthenticationError("Can't login")

    def start(self):
        """
            Crawler's run method
        """
        self._login()                

        for url in self.start_urls:
            self.pool.spawn_n(self._fetch, url, depth_level=0)

        self.pool.waitall()

    #overridables

    def get_urls(self, response):
        """
            Returns a list of urls found in the current html page
        """
        urls = []
        
        for url_match in self._url_regex.finditer(response.raw_html):
            
            urls.append(url_match.group(0))
        
        tree = XPathExtractor().get_object(response.raw_html)                
        
        for link_tag in tree.xpath("//a"):                
            
            if not 'href' in link_tag.attrib:
                continue
                
            url = link_tag.attrib["href"]
            
            if not self._url_regex.match(url):
                
                parsed_url = urlparse.urlparse(response.url)
                new_url = "%s://%s%s" % (parsed_url.scheme, parsed_url.netloc, url)                
                urls.append(new_url)
                
        return urls
