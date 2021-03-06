# Creates DNS zone files for all of the domains of all of the mail users
# and mail aliases and restarts nsd.
########################################################################

import os, os.path, urllib.parse, datetime, re, hashlib
import rtyaml

from mailconfig import get_mail_domains
from utils import shell, load_env_vars_from_file, safe_domain_name, sort_domains

def get_dns_domains(env):
	# Add all domain names in use by email users and mail aliases and ensure
	# PRIMARY_HOSTNAME is in the list.
	domains = set()
	domains |= get_mail_domains(env)
	domains.add(env['PRIMARY_HOSTNAME'])
	return domains

def get_dns_zones(env):
	# What domains should we create DNS zones for? Never create a zone for
	# a domain & a subdomain of that domain.
	domains = get_dns_domains(env)
	
	# Exclude domains that are subdomains of other domains we know. Proceed
	# by looking at shorter domains first.
	zone_domains = set()
	for domain in sorted(domains, key=lambda d : len(d)):
		for d in zone_domains:
			if domain.endswith("." + d):
				# We found a parent domain already in the list.
				break
		else:
			# 'break' did not occur: there is no parent domain.
			zone_domains.add(domain)

	# Make a nice and safe filename for each domain.
	zonefiles = []
	for domain in zone_domains:
		zonefiles.append([domain, safe_domain_name(domain) + ".txt"])

	# Sort the list so that the order is nice and so that nsd.conf has a
	# stable order so we don't rewrite the file & restart the service
	# meaninglessly.
	zone_order = sort_domains([ zone[0] for zone in zonefiles ], env)
	zonefiles.sort(key = lambda zone : zone_order.index(zone[0]) )

	return zonefiles
	

def do_dns_update(env):
	# What domains (and their zone filenames) should we build?
	domains = get_dns_domains(env)
	zonefiles = get_dns_zones(env)

	# Custom records to add to zones.
	try:
		additional_records = rtyaml.load(open(os.path.join(env['STORAGE_ROOT'], 'dns/custom.yaml')))
	except:
		additional_records = { }

	# Write zone files.
	os.makedirs('/etc/nsd/zones', exist_ok=True)
	updated_domains = []
	for i, (domain, zonefile) in enumerate(zonefiles):
		# Build the records to put in the zone.
		subdomains = [d for d in domains if d.endswith("." + domain)]
		records = build_zone(domain, subdomains, additional_records, env)

		# See if the zone has changed, and if so update the serial number
		# and write the zone file.
		if not write_nsd_zone(domain, "/etc/nsd/zones/" + zonefile, records, env):
			# Zone was not updated. There were no changes.
			continue

		# If this is a .justtesting.email domain, then post the update.
		try:
			justtestingdotemail(domain, records)
		except:
			# Hmm. Might be a network issue. If we stop now, will we end
			# up in an inconsistent state? Let's just continue.
			pass

		# Mark that we just updated this domain.
		updated_domains.append(domain)

		# Sign the zone.
		#
		# Every time we sign the zone we get a new result, which means
		# we can't sign a zone without bumping the zone's serial number.
		# Thus we only sign a zone if write_nsd_zone returned True
		# indicating the zone changed, and thus it got a new serial number.
		# write_nsd_zone is smart enough to check if a zone's signature
		# is nearing experiation and if so it'll bump the serial number
		# and return True so we get a chance to re-sign it.
		sign_zone(domain, zonefile, env)

	# Now that all zones are signed (some might not have changed and so didn't
	# just get signed now, but were before) update the zone filename so nsd.conf
	# uses the signed file.
	for i in range(len(zonefiles)):
		zonefiles[i][1] += ".signed"

	# Write the main nsd.conf file.
	if write_nsd_conf(zonefiles):
		# Make sure updated_domains contains *something* if we wrote an updated
		# nsd.conf so that we know to restart nsd.
		if len(updated_domains) == 0:
			updated_domains.append("DNS configuration")

	# Kick nsd if anything changed.
	if len(updated_domains) > 0:
		shell('check_call', ["/usr/sbin/service", "nsd", "restart"])

	# Write the OpenDKIM configuration tables.
	write_opendkim_tables(zonefiles, env)

	# Kick opendkim.
	shell('check_call', ["/usr/sbin/service", "opendkim", "restart"])

	if len(updated_domains) == 0:
		# if nothing was updated (except maybe OpenDKIM's files), don't show any output
		return ""
	else:
		return "updated DNS: " + ",".join(updated_domains) + "\n"

########################################################################

def build_zone(domain, subdomains, additional_records, env, with_ns=True):
	records = []

	# For top-level zones, define ourselves as the authoritative name server.
	if with_ns:
		records.append((None,  "NS",  "ns1.%s." % env["PRIMARY_HOSTNAME"]))
		records.append((None,  "NS",  "ns2.%s." % env["PRIMARY_HOSTNAME"]))

	# The MX record says where email for the domain should be delivered: Here!
	records.append((None,  "MX",  "10 %s." % env["PRIMARY_HOSTNAME"]))

	# SPF record: Permit the box ('mx', see above) to send mail on behalf of
	# the domain, and no one else.
	records.append((None,  "TXT", '"v=spf1 mx -all"'))

	# If we need to define DNS for any subdomains of this domain, include it
	# in the zone.
	for subdomain in subdomains:
		subdomain_qname = subdomain[0:-len("." + domain)]
		for child_qname, child_rtype, child_value in build_zone(subdomain, [], {}, env, with_ns=False):
			if child_qname == None:
				child_qname = subdomain_qname
			else:
				child_qname += "." + subdomain_qname
			records.append((child_qname, child_rtype, child_value))

	# In PRIMARY_HOSTNAME...
	if domain == env["PRIMARY_HOSTNAME"]:
		# Define ns1 and ns2.
		records.append(("ns1", "A",   env["PUBLIC_IP"]))
		records.append(("ns2", "A",   env["PUBLIC_IP"]))

		# Add a DANE TLSA record for SMTP.
		records.append(("_25._tcp", "TLSA", build_tlsa_record(env)))

	def has_rec(qname, rtype):
		for rec in records:
			if rec[0] == qname and rec[1] == rtype:
				return True
		return False

	# The user may set other records that don't conflict with our settings.
	for qname, value in additional_records.items():
		if qname != domain and not qname.endswith("." + domain): continue
		if qname == domain:
			qname = None
		else:
			qname = qname[0:len(qname)-len("." + domain)]
		if has_rec(qname, value): continue
		if isinstance(value, str):
			records.append((qname, "A", value))
		elif isinstance(value, dict):
			for rtype, value2 in value.items():
				if rtype == "TXT": value2 = "\"" + value2 + "\""
				records.append((qname, rtype, value2))

	# Add defaults if not overridden by the user's custom settings.
	if not has_rec(None, "A"): records.append((None, "A", env["PUBLIC_IP"]))
	if env.get('PUBLIC_IPV6') and not has_rec(None, "AAAA"): records.append((None, "AAAA", env["PUBLIC_IPV6"]))
	if not has_rec("www", "A"): records.append(("www", "A", env["PUBLIC_IP"]))
	if env.get('PUBLIC_IPV6') and not has_rec("www", "AAAA"): records.append(("www", "AAAA", env["PUBLIC_IPV6"]))

	# If OpenDKIM is in use..
	opendkim_record_file = os.path.join(env['STORAGE_ROOT'], 'mail/dkim/mail.txt')
	if os.path.exists(opendkim_record_file):
		# Append the DKIM TXT record to the zone as generated by OpenDKIM, after string formatting above.
		with open(opendkim_record_file) as orf:
			m = re.match(r"(\S+)\s+IN\s+TXT\s+(\(.*\))\s*;", orf.read(), re.S)
			records.append((m.group(1), "TXT", m.group(2)))

		# Append a DMARC record.
		records.append(("_dmarc", "TXT", '"v=DMARC1; p=quarantine"'))

	# Sort the records. The None records *must* go first. Otherwise it doesn't matter.
	records.sort(key = lambda rec : list(reversed(rec[0].split(".")) if rec[0] is not None else ""))

	return records

########################################################################

def build_tlsa_record(env):
	# A DANE TLSA record in DNS specifies that connections on a port
	# must use TLS and the certificate must match a particular certificate.
	#
	# Thanks to http://blog.huque.com/2012/10/dnssec-and-certificates.html
	# for explaining all of this!

	# Get the hex SHA256 of the DER-encoded server certificate:
	certder = shell("check_output", [
		"/usr/bin/openssl",
		"x509",
		"-in", os.path.join(env["STORAGE_ROOT"], "ssl", "ssl_certificate.pem"),
		"-outform", "DER"
		],
		return_bytes=True)
	certhash = hashlib.sha256(certder).hexdigest()

	# Specify the TLSA parameters:
	# 3: This is the certificate that the client should trust. No CA is needed.
	# 0: The whole certificate is matched.
	# 1: The certificate is SHA256'd here.
	return "3 0 1 " + certhash

########################################################################

def write_nsd_zone(domain, zonefile, records, env):
	# We set the administrative email address for every domain to domain_contact@[domain.com].
	# You should probably create an alias to your email address.

	# On the $ORIGIN line, there's typically a ';' comment at the end explaining
	# what the $ORIGIN line does. Any further data after the domain confuses
	# ldns-signzone, however. It used to say '; default zone domain'.

	zone = """
$ORIGIN {domain}.
$TTL 86400           ; default time to live

@ IN SOA ns1.{primary_domain}. hostmaster.{primary_domain}. (
           __SERIAL__     ; serial number
           28800       ; Refresh
           7200        ; Retry
           864000      ; Expire
           86400       ; Min TTL
           )
"""

	# Replace replacement strings.
	zone = zone.format(domain=domain, primary_domain=env["PRIMARY_HOSTNAME"])

	# Add records.
	for subdomain, querytype, value in records:
		if subdomain:
			zone += subdomain
		zone += "\tIN\t" + querytype + "\t"
		zone += value + "\n"

	# DNSSEC requires re-signing a zone periodically. That requires
	# bumping the serial number even if no other records have changed.
	# We don't see the DNSSEC records yet, so we have to figure out
	# if a re-signing is necessary so we can prematurely bump the
	# serial number.
	force_bump = False
	if not os.path.exists(zonefile + ".signed"):
		# No signed file yet. Shouldn't normally happen unless a box
		# is going from not using DNSSEC to using DNSSEC.
		force_bump = True
	else:
		# We've signed the domain. Check if we are close to the expiration
		# time of the signature. If so, we'll force a bump of the serial
		# number so we can re-sign it.
		with open(zonefile + ".signed") as f:
			signed_zone = f.read()
		expiration_times = re.findall(r"\sRRSIG\s+SOA\s+\d+\s+\d+\s\d+\s+(\d{14})", signed_zone)
		if len(expiration_times) == 0:
			# weird
			force_bump = True
		else:
			# All of the times should be the same, but if not choose the soonest.
			expiration_time = min(expiration_times)
			expiration_time = datetime.datetime.strptime(expiration_time, "%Y%m%d%H%M%S")
			if expiration_time - datetime.datetime.now() < datetime.timedelta(days=3):
				# We're within three days of the expiration, so bump serial & resign.
				force_bump = True

	# Set the serial number.
	serial = datetime.datetime.now().strftime("%Y%m%d00")
	if os.path.exists(zonefile):
		# If the zone already exists, is different, and has a later serial number,
		# increment the number.
		with open(zonefile) as f:
			existing_zone = f.read()
			m = re.search(r"(\d+)\s*;\s*serial number", existing_zone)
			if m:
				# Clear out the serial number in the existing zone file for the
				# purposes of seeing if anything *else* in the zone has changed.
				existing_serial = m.group(1)
				existing_zone = existing_zone.replace(m.group(0), "__SERIAL__     ; serial number")

				# If the existing zone is the same as the new zone (modulo the serial number),
				# there is no need to update the file. Unless we're forcing a bump.
				if zone == existing_zone and not force_bump:
					return False

				# If the existing serial is not less than a serial number
				# based on the current date plus 00, increment it. Otherwise,
				# the serial number is less than our desired new serial number
				# so we'll use the desired new number.
				if existing_serial >= serial:
					serial = str(int(existing_serial) + 1)

	zone = zone.replace("__SERIAL__", serial)

	# Write the zone file.
	with open(zonefile, "w") as f:
		f.write(zone)

	return True # file is updated

########################################################################

def write_nsd_conf(zonefiles):
	# Basic header.
	nsdconf = """
server:
  hide-version: yes

  # identify the server (CH TXT ID.SERVER entry).
  identity: ""

  # The directory for zonefile: files.
  zonesdir: "/etc/nsd/zones"
"""
	
	# Since we have bind9 listening on localhost for locally-generated
	# DNS queries that require a recursive nameserver, we must have
	# nsd listen only on public network interfaces. Those interfaces
	# may have addresses different from the public IP address that the
	# Internet sees this machine on. Get those interface addresses
	# from `hostname -i` (which omits all localhost addresses).
	for ipaddr in shell("check_output", ["/bin/hostname", "-I"]).strip().split(" "):
		nsdconf += "  ip-address: %s\n" % ipaddr


	# Append the zones.
	for domain, zonefile in zonefiles:
		nsdconf += """
zone:
	name: %s
	zonefile: %s
""" % (domain, zonefile)

	# Check if the nsd.conf is changing. If it isn't changing,
	# return False to flag that no change was made.
	with open("/etc/nsd/nsd.conf") as f:
		if f.read() == nsdconf:
			return False

	with open("/etc/nsd/nsd.conf", "w") as f:
		f.write(nsdconf)

	return True

########################################################################

def sign_zone(domain, zonefile, env):
	dnssec_keys = load_env_vars_from_file(os.path.join(env['STORAGE_ROOT'], 'dns/dnssec/keys.conf'))

	# In order to use the same keys for all domains, we have to generate
	# a new .key file with a DNSSEC record for the specific domain. We
	# can reuse the same key, but it won't validate without a DNSSEC
	# record specifically for the domain.
	# 
	# Copy the .key and .private files to /tmp to patch them up.
	#
	# Use os.umask and open().write() to securely create a copy that only
	# we (root) can read.
	files_to_kill = []
	for key in ("KSK", "ZSK"):
		if dnssec_keys.get(key, "").strip() == "": raise Exception("DNSSEC is not properly set up.")
		oldkeyfn = os.path.join(env['STORAGE_ROOT'], 'dns/dnssec/' + dnssec_keys[key])
		newkeyfn = '/tmp/' + dnssec_keys[key].replace("_domain_", domain)
		dnssec_keys[key] = newkeyfn
		for ext in (".private", ".key"):
			if not os.path.exists(oldkeyfn + ext): raise Exception("DNSSEC is not properly set up.")
			with open(oldkeyfn + ext, "r") as fr:
				keydata = fr.read()
			keydata = keydata.replace("_domain_", domain) # trick ldns-signkey into letting our generic key be used by this zone
			fn = newkeyfn + ext
			prev_umask = os.umask(0o77) # ensure written file is not world-readable
			try:
				with open(fn, "w") as fw:
					fw.write(keydata)
			finally:
				os.umask(prev_umask) # other files we write should be world-readable
			files_to_kill.append(fn)

	# Do the signing.
	expiry_date = (datetime.datetime.now() + datetime.timedelta(days=30)).strftime("%Y%m%d")
	shell('check_call', ["/usr/bin/ldns-signzone",
		# expire the zone after 30 days
		"-e", expiry_date,

		# use NSEC3
		"-n",

		# zonefile to sign
		"/etc/nsd/zones/" + zonefile,

		# keys to sign with (order doesn't matter -- it'll figure it out)
		dnssec_keys["KSK"],
		dnssec_keys["ZSK"],
	])

	# Create a DS record based on the patched-up key files. The DS record is specific to the
	# zone being signed, so we can't use the .ds files generated when we created the keys.
	# The DS record points to the KSK only. Write this next to the zone file so we can
	# get it later to give to the user with instructions on what to do with it.
	rr_ds = shell('check_output', ["/usr/bin/ldns-key2ds",
		"-n", # output to stdout
		"-2", # SHA256
		dnssec_keys["KSK"] + ".key"
	])
	with open("/etc/nsd/zones/" + zonefile + ".ds", "w") as f:
		f.write(rr_ds)

	# Remove our temporary file.
	for fn in files_to_kill:
		os.unlink(fn)

########################################################################

def get_ds_records(env):
	zonefiles = get_dns_zones(env)
	ret = ""
	for domain, zonefile in zonefiles:
		fn = "/etc/nsd/zones/" + zonefile + ".ds"
		if os.path.exists(fn):
			with open(fn, "r") as fr:
				ret += fr.read().strip() + "\n"
	return ret
	
	
########################################################################

def write_opendkim_tables(zonefiles, env):
	# Append a record to OpenDKIM's KeyTable and SigningTable for each domain.
	#
	# The SigningTable maps email addresses to signing information. The KeyTable
	# maps specify the hostname, the selector, and the path to the private key.
	#
	# DKIM ADSP and DMARC both only support policies where the signing domain matches
	# the From address, so the KeyTable must specify that the signing domain for a
	# sender matches the sender's domain.
	#
	# In SigningTable, we map every email address to a key record named after the domain.
	# Then we specify for the key record its domain, selector, and key.

	opendkim_key_file = os.path.join(env['STORAGE_ROOT'], 'mail/dkim/mail.private')
	if not os.path.exists(opendkim_key_file): return

	with open("/etc/opendkim/KeyTable", "w") as f:
		f.write("\n".join(
			"{domain} {domain}:mail:{key_file}".format(domain=domain, key_file=opendkim_key_file)
			for domain, zonefile in zonefiles
		))

	with open("/etc/opendkim/SigningTable", "w") as f:
		f.write("\n".join(
			"*@{domain} {domain}".format(domain=domain)
			for domain, zonefile in zonefiles
		))

########################################################################

def justtestingdotemail(domain, records):
	# If the domain is a subdomain of justtesting.email, which we own,
	# automatically populate the zone where it is set up on dns4e.com.
	# Ideally if dns4e.com supported NS records we would just have it
	# delegate DNS to us, but instead we will populate the whole zone.

	import subprocess, json, urllib.parse

	if not domain.endswith(".justtesting.email"):
		return

	for subdomain, querytype, value in records:
		if querytype in ("NS",): continue
		if subdomain in ("www", "ns1", "ns2"): continue # don't do unnecessary things

		if subdomain == None:
			subdomain = domain
		else:
			subdomain = subdomain + "." + domain

		if querytype == "TXT":
			# nsd requires parentheses around txt records with multiple parts,
			# but DNS4E requires there be no parentheses; also it goes into
			# nsd with a newline and a tab, which we replace with a space here
			value = re.sub("^\s*\(\s*([\w\W]*)\)", r"\1", value)
			value = re.sub("\s+", " ", value)
		else:
			continue

		print("Updating DNS for %s/%s..." % (subdomain, querytype))
		resp = json.loads(subprocess.check_output([
			"curl",
			"-s",
			"https://api.dns4e.com/v7/%s/%s" % (urllib.parse.quote(subdomain), querytype.lower()),
			"--user", "2ddbd8e88ed1495fa0ec:A97TDJV26CVUJS6hqAs0CKnhj4HvjTM7MwAAg8xb",
			"--data", "record=%s" % urllib.parse.quote(value),
			]).decode("utf8"))
		print("\t...", resp.get("message", "?"))
