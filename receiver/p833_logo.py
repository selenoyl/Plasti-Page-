# The PLASTI-PAGE logo, for the receiver's pages. Made by tools/make_logo.py: don't edit by hand.
# By Ethan Brady & Ryan Raikes
#
# Styled after the EPC logo: a red square behind a blue globe (here with a paging horn), then
# the name in bold blue capitals. The letters are outlines of Liberation Sans Bold (SIL Open
# Font License), so the logo looks the same on every device.

from urllib.parse import quote

WIDTH, HEIGHT = 950, 112
BLUE, RED, LIGHT = "#004594", "#C6002A", "#E4E5F1"
MARK = '<rect x="5" y="5" width="102" height="102" fill="#C6002A"/><circle cx="56" cy="56" r="55" fill="#004594"/><g fill="none" stroke="#fff" stroke-width="1.6"><line x1="56" y1="1" x2="56" y2="111"/><ellipse cx="56" cy="56" rx="27.5" ry="55"/><ellipse cx="56" cy="56" rx="47.6" ry="55"/><line x1="1" y1="56" x2="111" y2="56"/><line x1="6.2" y1="79.3" x2="105.8" y2="79.3"/><line x1="6.2" y1="32.7" x2="105.8" y2="32.7"/><line x1="20.6" y1="98.1" x2="91.4" y2="98.1"/><line x1="20.6" y1="13.9" x2="91.4" y2="13.9"/></g><g fill="#E4E5F1" stroke="#004594" stroke-width="2.4" stroke-linejoin="round"><rect x="22" y="47" width="12" height="18" rx="2.5"/><path d="M34 50 L62 34 Q67 56 62 78 L34 62 Z"/></g><g fill="none" stroke="#E4E5F1" stroke-width="5" stroke-linecap="round"><path d="M72.7 49.2 A11 11 0 0 1 72.7 62.8"/><path d="M80.5 43.1 A21 21 0 0 1 80.5 68.9"/><path d="M88.4 36.9 A31 31 0 0 1 88.4 75.1"/></g>'  # noqa: E501
WORDMARK = '<path d="M216.1 40.6Q216.1 48.7 212.4 55.1Q208.7 61.5 201.8 64.9Q194.9 68.4 185.5 68.4H164.6V98H147V14H184.7Q199.8 14 208 20.9Q216.1 27.9 216.1 40.6ZM198.4 40.9Q198.4 27.7 182.8 27.7H164.6V54.9H183.2Q190.5 54.9 194.5 51.3Q198.4 47.7 198.4 40.9ZM228.4 98V14H246V84.4H291.1V98ZM362.4 98 354.9 76.5H322.9L315.5 98H297.9L328.5 14H349.3L379.8 98ZM338.9 26.9 338.5 28.2Q338 30.4 337.1 33.1Q336.3 35.9 326.9 63.3H351L342.7 39.2L340.2 31.1ZM459.7 73.8Q459.7 86.1 450.5 92.7Q441.4 99.2 423.7 99.2Q407.5 99.2 398.3 93.5Q389.2 87.7 386.5 76.1L403.5 73.3Q405.3 80 410.3 83Q415.3 86 424.2 86Q442.6 86 442.6 74.8Q442.6 71.2 440.5 68.9Q438.3 66.6 434.5 65Q430.7 63.5 419.7 61.3Q410.3 59.1 406.6 57.7Q402.9 56.4 400 54.6Q397 52.8 394.9 50.2Q392.8 47.6 391.6 44.2Q390.5 40.7 390.5 36.2Q390.5 24.9 399 18.8Q407.6 12.7 423.9 12.7Q439.5 12.7 447.4 17.6Q455.2 22.5 457.5 33.8L440.4 36.1Q439.1 30.7 435.1 28Q431.1 25.2 423.6 25.2Q407.6 25.2 407.6 35.2Q407.6 38.5 409.3 40.6Q411 42.7 414.3 44.1Q417.7 45.6 427.9 47.8Q440 50.4 445.2 52.5Q450.4 54.7 453.4 57.6Q456.5 60.5 458.1 64.5Q459.7 68.5 459.7 73.8ZM510.5 27.6V98H493V27.6H465.8V14H537.7V27.6ZM547.2 98V14H564.8V98ZM577.7 73.6V59.1H608.7V73.6ZM690.9 40.6Q690.9 48.7 687.2 55.1Q683.5 61.5 676.6 64.9Q669.7 68.4 660.2 68.4H639.4V98H621.8V14H659.5Q674.6 14 682.7 20.9Q690.9 27.9 690.9 40.6ZM673.2 40.9Q673.2 27.7 657.6 27.7H639.4V54.9H658Q665.3 54.9 669.2 51.3Q673.2 47.7 673.2 40.9ZM753.5 98 746.1 76.5H714.1L706.6 98H689L719.7 14H740.4L770.9 98ZM730.1 26.9 729.7 28.2Q729.1 30.4 728.3 33.1Q727.4 35.9 718 63.3H742.2L733.9 39.2L731.3 31.1ZM822.2 85.4Q829.1 85.4 835.5 83.4Q842 81.4 845.5 78.3V66.7H825V53.7H861.6V84.6Q854.9 91.4 844.2 95.3Q833.5 99.2 821.7 99.2Q801.2 99.2 790.2 87.8Q779.2 76.5 779.2 55.6Q779.2 34.9 790.3 23.8Q801.4 12.7 822.2 12.7Q851.7 12.7 859.8 34.6L843.6 39.5Q840.9 33.1 835.3 29.9Q829.7 26.6 822.2 26.6Q809.8 26.6 803.3 34.1Q796.9 41.6 796.9 55.6Q796.9 69.9 803.5 77.6Q810.2 85.4 822.2 85.4ZM877.3 98V14H943.4V27.6H894.9V48.7H939.7V62.3H894.9V84.4H945.8V98Z"/>'  # noqa: E501


def svg(css_class="logo"):
    """The logo as inline SVG. It's hidden from screen readers: the page's heading names it."""
    return ('<svg class="%s" viewBox="0 0 %d %d" width="%d" height="%d" aria-hidden="true" focusable="false">'
            '%s<g class="wm" fill="%s">%s</g></svg>' % (css_class, WIDTH, HEIGHT, WIDTH, HEIGHT, MARK, BLUE, WORDMARK))


# The mark alone, as the browser tab's icon.
FAVICON = "data:image/svg+xml," + quote('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d">%s</svg>'
                                        % (HEIGHT, HEIGHT, MARK), safe="")
