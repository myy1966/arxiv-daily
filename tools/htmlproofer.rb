# frozen_string_literal: true

# Run the stock `htmlproofer` CLI (same arguments) with a faster internal
# hash check.
#
# html-proofer 5.x checks each `#fragment` by running a full-document XPath
# with a Ruby callback per node, so a page costs (hashes x nodes). Our big
# daily posts have ~1.4k hashes and ~25k nodes, which made the check take over
# an hour. Here each target page is scanned once into a Set of id/name values,
# and each hash becomes a lookup. Results are the same: an exact,
# case-sensitive match on `id` or `name`, for the raw and the URL-decoded hash.
#
# Usage: bundle exec ruby tools/htmlproofer.rb <htmlproofer args...>

require "html-proofer"
require "set"

module FastFragmentLookup
  private def hash_exists_in_html?(href_hash, html)
    # Hashes are checked one target file at a time, so caching only the
    # current document is enough and keeps earlier pages free to be GC'd.
    unless @fragment_doc.equal?(html)
      @fragment_doc = html
      @fragment_ids = Set.new
      html.xpath("//*[@id or @name]").each do |node|
        @fragment_ids << node["id"] if node["id"]
        @fragment_ids << node["name"] if node["name"]
      end
    end

    @fragment_ids.include?(href_hash) ||
      @fragment_ids.include?(Addressable::URI.unescape(href_hash))
  end
end

internal = HTMLProofer::UrlValidator::Internal
if internal.private_method_defined?(:hash_exists_in_html?)
  internal.prepend(FastFragmentLookup)
else
  # Not `warn`: that is silenced under `ruby -W0`, and this should always show.
  $stderr.puts "tools/htmlproofer.rb: HTMLProofer::UrlValidator::Internal#hash_exists_in_html? " \
               "not found in html-proofer #{HTMLProofer::VERSION}; running without the fast hash check."
end

load Gem.bin_path("html-proofer", "htmlproofer")
