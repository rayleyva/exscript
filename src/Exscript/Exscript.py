import sys, time, os, re, signal, gc, copy
from Interpreter     import Parser
from FooLib          import UrlParser
from SpiffWorkQueue  import WorkQueue
from SpiffWorkQueue  import Sequence
from TerminalActions import *

True  = 1
False = 0

class Exscript(object):
    """
    This is an API for accessing all of Exscript's functions programmatically.
    This may still need some cleaning up, so don't count on API stability 
    just yet.
    """
    bracket_expression_re = re.compile(r'^\{([^\]]*)\}$')

    def __init__(self, **kwargs):
        """
        Constructor.

        kwargs: verbose: The verbosity level of the interpreter.
                parser_verbose: The verbosity level of the parser.
                domain: The default domain of the contacted hosts.
                logdir: The directory into which the logs are written.
                no_prompt: Whether the compiled program should wait for a 
                           prompt each time after the Exscript sent a 
                           command to the remote host.
        """
        self.workqueue      = WorkQueue()
        self.exscript       = None
        self.exscript_code  = None
        self.exscript_file  = None
        self.hostnames      = []
        self.host_defines   = {}
        self.global_defines = {}
        self.verbose        = kwargs.get('verbose')
        self.logdir         = kwargs.get('logdir')
        self.overwrite_logs = kwargs.get('overwrite_logs', False)
        self.domain         = kwargs.get('domain',         '')
        self.parser         = Parser(debug     = kwargs.get('parser_verbose', 0),
                                     no_prompt = kwargs.get('no_prompt',      0))

        self.workqueue.signal_connect('job-started',   self._on_job_started)
        self.workqueue.signal_connect('job-completed', self._on_job_completed)


    def _on_job_started(self, job):
        if self.workqueue.get_max_threads() > 1:
            print job.getName(), 'started.'


    def _on_job_completed(self, job):
        if self.workqueue.get_max_threads() > 1:
            print job.getName(), 'completed.'


    def _dbg(self, level, msg):
        if level > self.verbose:
            return
        print msg


    def add_host(self, host):
        """
        Adds a single given host for executing the script later.
        """
        self.hostnames.append(host)
        url = UrlParser.parse_url(host)
        for key, val in url.vars.iteritems():
            match = Exscript.bracket_expression_re.match(val[0])
            if match is None:
                continue
            string = match.group(1) or 'a value for "%s"' % key
            val    = raw_input('Please enter %s: ' % string)
            url.vars[key] = [val]
        self.host_defines[host] = url.vars


    def add_hosts(self, hosts):
        """
        Adds the given list of hosts for executing the script later.
        """
        for host in hosts:
            self.add_host(host)


    def add_hosts_from_file(self, filename):
        """
        Reads a list of hostnames from the file with the given name.
        """
        # Open the file.
        if not os.path.exists(filename):
            raise IOError('No such file: %s' % filename)
        file_handle = open(filename, 'r')

        # Read the hostnames.
        for line in file_handle:
            hostname = line.strip()
            if hostname == '':
                continue
            self.add_host(hostname)

        file_handle.close()


    def add_hosts_from_csv(self, filename):
        """
        Reads a list of hostnames and variables from the .csv file with the 
        given name.
        """
        # Open the file.
        if not os.path.exists(filename):
            raise IOError('No such file: %s' % filename)
        file_handle = open(filename, 'r')

        # Read the header.
        header = file_handle.readline().rstrip()
        if re.search(r'^hostname\b', header) is None:
            msg  = 'Syntax error in CSV file header:'
            msg += ' File does not start with "hostname".'
            raise Exception(msg)
        if re.search(r'^hostname(?:\t[^\t]+)*$', header) is None:
            msg  = 'Syntax error in CSV file header:'
            msg += ' Make sure to separate columns by tabs.'
            raise Exception(msg)
        varnames = header.split('\t')
        varnames.pop(0)
        
        # Walk through all lines and create a map that maps hostname to definitions.
        last_hostname = ''
        for line in file_handle:
            line         = re.sub(r'[\r\n]*$', '', line)
            values       = line.split('\t')
            hostname_url = values.pop(0).strip()
            hostname     = UrlParser.parse_url(hostname_url).hostname

            # Add the hostname to our list.
            if hostname != last_hostname:
                #print "Reading hostname", hostname, "from csv."
                self.add_host(hostname_url)
                last_hostname = hostname

            # Define variables according to the definition.
            for i in range(0, len(varnames)):
                varname = varnames[i]
                try:
                    value = values[i]
                except:
                    value = ''
                if self.host_defines[hostname].has_key(varname):
                    self.host_defines[hostname][varname].append(value)
                else:
                    self.host_defines[hostname][varname] = [value]

        file_handle.close()


    def define(self, **kwargs):
        """
        Defines the given variables such that they may be accessed from 
        within the Exscript.
        """
        self.global_defines.update(kwargs)


    def define_host(self, hostname, **kwargs):
        """
        Defines the given variables such that they may be accessed from 
        within the Exscript.
        """
        if not self.host_defines.has_key(hostname):
            self.host_defines[hostname] = {}
        self.host_defines[hostname].update(kwargs)


    def load(self, exscript_content):
        """
        Loads the given Exscript code, using the given options.
        MUST be called before run() is called.
        """
        # Parse the exscript.
        self.parser.define(**self.global_defines)
        self.parser.define(**self.host_defines[self.hostnames[0]])
        self.parser.define(__filename__ = self.exscript_file)
        self.parser.define(hostname = self.hostnames[0])
        try:
            self.exscript = self.parser.parse(exscript_content)
	    self.exscript_code = exscript_content
        except Exception, e:
            if self.verbose > 0:
                raise
            print e
            sys.exit(1)


    def load_from_file(self, filename):
        """
        Loads the Exscript file with the given name, and calls load() to 
        process the code using the given options.
        """
        file_handle = open(filename, 'r')
        self.exscript_file = filename
        exscript_content   = file_handle.read()
        file_handle.close()
        self.load(exscript_content)


    def _new_job(self, hostname, **kwargs):
        """
        Compiles the current exscript, and returns a new workqueue sequence 
        for it that is initialized and has all the variables defined.
        """
        # Prepare variables that are passed to the Exscript interpreter.
        user             = kwargs.get('user')
        password         = kwargs.get('password')
        default_protocol = kwargs.get('protocol', 'telnet')
        url              = UrlParser.parse_url(hostname, default_protocol)
        this_proto       = url.protocol
        this_user        = url.username
        this_password    = url.password
        this_host        = url.hostname
        if not '.' in this_host and len(self.domain) > 0:
            this_host += '.' + self.domain
        variables = dict()
        variables.update(self.global_defines)
        variables.update(self.host_defines[hostname])
        variables['hostname'] = this_host
        variables.update(url.vars)
        if this_user is None:
            this_user = user
        if this_password is None:
            this_password = password

        #FIXME: In Python > 2.2 we can (hopefully) deep copy the object instead of
        # recompiling numerous times.
        self.parser.define(**variables)
        if kwargs.has_key('filename'):
            exscript = self.parser.parse_file(kwargs.get('filename'))
        else:
            exscript_code = kwargs.get('code', self.exscript_code)
            exscript      = self.parser.parse(exscript_code)
        #exscript = copy.deepcopy(self.exscript)
        exscript.init(**variables)
        exscript.define(__filename__ = self.exscript_file)
        exscript.define(__exscript__ = self)

        # One logfile per host.
        logfile       = None
        error_logfile = None
        if self.logdir is None:
            sequence = Sequence(name = this_host)
        else:
            logfile       = os.path.join(self.logdir, this_host + '.log')
            error_logfile = logfile + '.error'
            overwrite     = self.overwrite_logs
            sequence      = LoggedSequence(name          = this_host,
                                           logfile       = logfile,
                                           error_logfile = error_logfile,
                                           overwrite_log = overwrite)

        # Choose the protocol.
        if this_proto == 'telnet':
            protocol = __import__('termconnect.Telnet',
                                  globals(),
                                  locals(),
                                  'Telnet')
        elif this_proto in ('ssh', 'ssh1', 'ssh2'):
            protocol = __import__('termconnect.SSH',
                                  globals(),
                                  locals(),
                                  'SSH')
        else:
            print 'Unsupported protocol %s' % this_proto
            return None

        # Build the sequence.
        noecho       = kwargs.get('no-echo',           False)
        key          = kwargs.get('ssh-key',           None)
        av           = kwargs.get('ssh-auto-verify',   None)
        nip          = kwargs.get('no-initial-prompt', False)
        nop          = kwargs.get('no-prompt',         False)
        authenticate = not kwargs.get('no-authentication', False)
        echo         = kwargs.get('connections', 1) == 1 and not noecho
        wait         = not nip and not nop
        if this_proto == 'ssh1':
            ssh_version = 1
        elif this_proto == 'ssh2':
            ssh_version = 2
        else:
            ssh_version = None # auto-select
        protocol_args = {'echo':        echo,
                         'auto_verify': av,
                         'ssh_version': ssh_version}
        if url.port is not None:
            protocol_args['port'] = url.port
        sequence.add(Connect(protocol, this_host, **protocol_args))
        if key is None and authenticate:
            sequence.add(Authenticate(this_user,
                                      password = this_password,
                                      wait     = wait))
        elif authenticate:
            sequence.add(Authenticate(this_user,
                                      key_file = key,
                                      wait     = wait))
        sequence.add(CommandScript(exscript))
        sequence.add(Close())

        if kwargs.get('priority') == 'force':
            self.workqueue.priority_enqueue(sequence, True)
        elif kwargs.get('priority') == 'high':
            self.workqueue.priority_enqueue(sequence)
        else:
            self.workqueue.enqueue(sequence)
        return sequence


    def _run(self, **kwargs):
        """
        Executes the currently loaded Exscript file on the currently added 
        hosts.
        """
        if self.exscript is None:
            msg = 'An Exscript was not yet loaded using load().'
            raise Exception(msg)

        # Initialize the workqueue.
        n_connections = kwargs.get('connections', 1)
        self.workqueue.set_max_threads(n_connections)
        self.workqueue.set_debug(kwargs.get('verbose', 0))

        self._dbg(1, 'Starting engine...')
        self.workqueue.start()
        self._dbg(1, 'Engine running.')

        # Build the action sequence.
        self._dbg(1, 'Building sequence...')
        for hostname in self.hostnames[:]:
            # To save memory, limit the number of parsed (=in-memory) items.
            while self.workqueue.get_length() > n_connections * 2:
                time.sleep(1)
                gc.collect()

            self._dbg(1, 'Building sequence for %s.' % hostname)
            self._new_job(hostname, **kwargs)

        # Wait until the engine is finished.
        self._dbg(1, 'All actions enqueued.')
        while self.workqueue.get_length() > 0:
            #print '%s jobs left, waiting.' % workqueue.get_length()
            time.sleep(1)
            gc.collect()
        self._dbg(1, 'Shutting down engine...')


    def run(self, **kwargs):
        """
        Executes the currently loaded Exscript file on the currently added 
        hosts. Allows for interrupting with SIGINT.
        """
        # Make sure that we shut down properly even when SIGINT or SIGTERM is sent.
        def on_posix_signal(signum, frame):
            print '************ SIGINT RECEIVED - SHUTTING DOWN! ************'
            raise KeyboardInterrupt

        signal.signal(signal.SIGINT,  on_posix_signal)
        signal.signal(signal.SIGTERM, on_posix_signal)

        try:
            self._run(**kwargs)
        except KeyboardInterrupt:
            print 'Interrupt caught succcessfully.'
            print '%s unfinished jobs.' % self.workqueue.get_length()
            sys.exit(1)

        self.workqueue.shutdown()
        self._dbg(1, 'Engine shut down.')
