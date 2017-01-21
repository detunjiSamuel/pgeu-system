from django.http import Http404, HttpResponse
from django.template.backends.utils import csrf_input_lazy, csrf_token_lazy
from django.template import defaultfilters
from django.utils.text import slugify
from django.conf import settings

import json
import os.path
import random
from itertools import groupby

import jinja2
import jinja2.sandbox
import markdown


from postgresqleu.confreg.templatetags.leadingnbsp import leadingnbsp

# We use a separate root directory for jinja2 templates, so find that
# directory by searching relative to ourselves.
TEMPLATE_ROOT=os.path.abspath(os.path.join(os.path.dirname(__file__), '../../template.jinja'))


# Locate the git revision for a repository in the given path, including
# walking up the tree to find it if the specified path is not the root.
def find_git_revision(path):
	while path != '/':
		if os.path.exists(os.path.join(path, ".git/HEAD")):
			# Found it!
			with open(os.path.join(path, '.git/HEAD')) as f:
				ref = f.readline().strip()
			if not ref.startswith('ref: refs/heads/'):
				return None
			refname = os.path.join(path, ".git/", ref[5:])
			if not os.path.isfile(refname):
				return None
			with open(refname) as f:
				fullref = f.readline()
				return fullref[:7]
		# Else step up one level
		path = os.path.dirname(path)
	return None


#
# A template loader specifically for confreg. It will
#  - load user-supplied templates from the specified conferences's
#    <jinjadir>/templates (and subdirectories)
#  - the specified template from the confreg namespace (but *not* other templates
#    in the conference namespace)
#  - specific whitelisted templates elsewhere
#
# This will make it impossible for a user-supplied templates to "break out"
# by including or inheriting templates from other parts of the system.
class ConfTemplateLoader(jinja2.FileSystemLoader):
	# Templates that are whitelisted for inclusion.
	WHITELISTED_TEMPLATES=('invoices/userinvoice_spec.html',)

	def __init__(self, conference, roottemplate):
		self.conference = conference
		self.roottemplate = roottemplate
		super(ConfTemplateLoader, self).__init__([os.path.join(conference.jinjadir, 'templates'), TEMPLATE_ROOT])

	def get_source(self, environment, template):
		# Only allow loading of the root template from confreg. Everything else we allow
		# only from the conference specific directory. This is so we don't end up
		# loading a template with the wrong parameters passed to it.
		if template != self.roottemplate:
			if not os.path.exists(os.path.join(self.conference.jinjadir, 'templates', template)):
				# This template may exist in pgeu, so reject it unless it's specifically
				# whitelisted as something we want to load.
				if not template in self.WHITELISTED_TEMPLATES:
					raise jinja2.TemplateNotFound(template, "Rejecting attempt to load from incorrect location")
		return super(ConfTemplateLoader, self).get_source(environment, template)


#
# A jinja2 sandbox for rendering confreg templates.
#
# It's designed for confreg only, and as such applies a number of restrictions on
# which attributes can be accessed of the objects that's passed to it.
#
# - Restrictions are applied to all pgeu models:
#   - For any models outside the confreg and confwiki namespaces, only attributes
#     specifically listed in the models _safe_attributes are allowed.
#   - The same applies to any model wihin confreg that has a _safe_attributes set
#   - Any model that has a member named conference are considered part oc confreg,
#     and access will be allowed to all attributes on it.
#     - Except if it has a member called _unsafe_attributes, in which case they are
#       restricted.
#   - Specifically for InvoicePresentationWrapper, access is allowed except for
#     things listed in _unsafe_attributes.
#
# For all other access, the jinja2 default sanbox rules apply.
#
class ConfSandbox(jinja2.sandbox.SandboxedEnvironment):
	def is_safe_attribute(self, obj, attr, value):
		modname = obj.__class__.__module__

		if obj.__class__.__name__ in ('str', 'unicode') and attr == 'format':
			# We reject all format strings for now, due to
			# https://www.palletsprojects.com/blog/jinja-281-released/
			# (until we have it safely patched everywhere, *if* we need this elsewhere)
			return False

		if modname.startswith('postgresqleu.') and modname.endswith('models'):
			# This is a pgeu model. So we only allow access to the
			# ones in confreg directly.
			if not (modname.endswith('.confreg.models') or modname.endswith('confwiki.models')):
				# If the object lists a number of safe attributes,
				# then allow them and nothing else.
				if hasattr(obj, '_safe_attributes'):
					if not attr in getattr(obj, '_safe_attributes'):
						return False
				else:
					# No safe attributes specified, so assume none
					return False

			# Some objects in the confreg model are not safe, because
			# they might leak data between conferences. In general,
			# these are objects that don't have a link to a
			# conference.
			if not hasattr(obj, 'conference'):
				if hasattr(obj, '_safe_attributes'):
					# If the object lists a number of safe attributes,
					# then allow them and nothing else.
					if not attr in getattr(obj, '_safe_attributes'):
						return False
				else:
					return False
			else:
				# Has a conference, but we can still specify unsafe ones
				if hasattr(obj, '_unsafe_attributes'):
					if attr in getattr(obj, '_unsafe_attributes'):
						return False
		elif modname=='postgresqleu.invoices.util' and obj.__class__.__name__=='InvoicePresentationWrapper':
			# This is ugly, but we special-case the invoice information
			if attr in obj._unsafe_attributes:
				return False

		return super(ConfSandbox, self).is_safe_attribute(obj, attr, value)


# A couple of useful filters that we publish everywhere:

# Like |groupby, except support grouping by objects and not just by values, and sort by
# attributes on the grouped objects.
def filter_groupby_sort(objects, keyfield, sortkey):
	group = [(key, list(group)) for key, group in groupby(objects, lambda x: getattr(x, keyfield))]
	return sorted(group, key=lambda y: y[0] and getattr(y[0], sortkey) or None)

# Shuffle the order in a list, for example to randomize the order of sponsors
def filter_shuffle(l):
	try:
		r = list(l)
		random.shuffle(r)
		return r
	except:
		return l

def filter_currency_format(v):
	return u"{0} {1:,.2f}".format(unicode(settings.CURRENCY_SYMBOL, 'utf8'), v)

def filter_float_str(f, n):
	return '{{0:.{0}f}}'.format(int(n)).format(f)


# Render a conference response based on jinja2 templates configured for the conference.
# Returns the appropriate django HttpResponse object.
def render_jinja_conference_response(request, conference, pagemagic, templatename, dictionary):
	# It all starts from the base template for this conference. If it
	# does not exist, just throw a 404 early.
	if not os.path.exists(os.path.join(conference.jinjadir, 'templates/base.html')):
		raise Http404()

	env = ConfSandbox(loader=ConfTemplateLoader(conference, templatename))
	env.filters.update({
		'currency_format': filter_currency_format,
		'escapejs': defaultfilters.escapejs_filter,
		'floatstr': filter_float_str,
		'groupby_sort': filter_groupby_sort,
		'leadingnbsp': leadingnbsp,
		'markdown': lambda t: jinja2.Markup(markdown.markdown(t)),
		'shuffle': filter_shuffle,
		'slugify': slugify,
		'yesno': lambda b,v: v.split(',')[b and 0 or 1],
	})

	# If ?test=1 is specified, try to load a template with .test in the
	# name.
	if request.GET.get('test', None) == '1':
		templatename = templatename + '.test'
	t = env.get_template(templatename)

	# Optionally load the JSON context with template-specific data
	if os.path.exists(os.path.join(conference.jinjadir, 'templates/context.json')):
		try:
			with open(os.path.join(conference.jinjadir, 'templates/context.json')) as f:
				c = json.load(f)
		except ValueError, e:
			return HttpResponse("JSON parse failed: {0}".format(e), content_type="text/plain")
		except Exception:
			c = {}
	else:
		c = {}

	c.update({
		'pgeu_hosted': True,
		'conference': conference,
		'pagemagic': pagemagic,
		'username': request.user and request.user.username or None,
		'githash': find_git_revision(conference.jinjadir),
		'csrf_input': csrf_input_lazy(request),
		'csrf_token': csrf_token_lazy(request),
	})
	if dictionary:
		c.update(dictionary)

	return HttpResponse(t.render(**c), content_type='text/html')