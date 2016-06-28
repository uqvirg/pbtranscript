#!/usr/bin/env python

"""Streaming IO support for Abundance files."""

from pbcore.io import ReaderBase, WriterBase

__all__ = ["AbundanceRecord",
           "AbundanceReader",
           "AbundanceWriter"]

TOTAL_FL = "# Total Number of FL reads:"
TOTAL_NFL = "# Total Number of FL + unique nFL reads:"
TOTAL_AMB = "# Total Number of all reads:"


class AbundanceRecord(object):

    """A AbundanceRecord contains the folliwing fields:
    pbid, count_fl, count_nfl, count_nfl_amb, norm_fl, norm_nfl, norm_nfl_amb
    where,
    count_fl: Number of associated FL reads
    count_nfl: Number of associated FL + unique nFL reads
    count_nfl_amb: Number of associated FL + unique nFL + weighted ambiguous nFL reads
    norm_fl: count_fl / total number of FL reads
    norm_nfl: count_nfl / total number of FL + unique nFL reads
    norm_nfl_amb: count_nfl_amb / total number of all reads
    """

    ATTRIBUTES = ["pbid", "count_fl", "count_nfl", "count_nfl_amb",
                  "norm_fl", "norm_nfl", "norm_nfl_amb"]
    HEADER = "\t".join(ATTRIBUTES)

    def __init__(self, pbid, count_fl, count_nfl, count_nfl_amb, norm_fl, norm_nfl, norm_nfl_amb):
        self.pbid = str(pbid)
        self.count_fl = int(count_fl)
        self.count_nfl = int(count_nfl)
        self.count_nfl_amb = float(count_nfl_amb)
        self.norm_fl = float(norm_fl)
        self.norm_nfl = float(norm_nfl)
        self.norm_nfl_amb = float(norm_nfl_amb)

    def __str__(self):
        return "{0}\t{1}\t{2}\t{3:.2f}\t{4:.4e}\t{5:.4e}\t{6:.4e}".format(
            self.pbid, self.count_fl, self.count_nfl, self.count_nfl_amb,
            self.norm_fl, self.norm_nfl, self.norm_nfl_amb)

    @classmethod
    def fromString(cls, line):
        """Construct and return a AbundanceRecord object given a string."""
        fields = line.strip().split('\t')
        if len(fields) != 7:
            raise ValueError("Could not recognize %s as a valid AbundanceRecord." % line)
        return AbundanceRecord(pbid=fields[0], count_fl=int(fields[1]), count_nfl=int(fields[2]),
                               count_nfl_amb=float(fields[3]), norm_fl=float(fields[4]),
                               norm_nfl=float(fields[5]), norm_nfl_amb=float(fields[6]))


class AbundanceReader(ReaderBase):

    """
    Streaming reader for an Abundance file.

    Example:

    .. doctest::
        >>> from pbtranscript.io import AbundanceReader
        >>> filename = "../../../tests/data/test_Abundance.txt"
        >>> for record in AbundanceReader(filename):
        ...     print record
    """
    def _read_comments_header(self):
        """Returns comments as well as the first line (usually header)."""
        comments = []
        firstLine = None
        for line in self.file:
            if line.startswith("#"):
                comments.append(line.rstrip())
            else:
                firstLine = line
                break
        return comments, firstLine

    @classmethod
    def parse_comments(cls, comments):
        """Returns total_fl, total_nfl, total_nfl_amb read from comments.
           total_fl = Total Number of FL reads
           total_nfl = Total Number of FL + unique nFL reads
           total_nfl_amb = Total Number of all reads
        """
        total_fl, total_nfl, total_nfl_amb = None, None, None
        if isinstance(comments, str):
            comments = comments.split("\n")
        elif not isinstance(comments, list):
            raise ValueError("comments %s must be either a str or a list of str" % comments)

        try:
            for h in comments:
                if TOTAL_FL in h:
                    total_fl = int(h.strip().split(":")[-1])
                elif TOTAL_NFL in h:
                    total_nfl = int(h.strip().split(":")[-1])
                elif TOTAL_AMB in h:
                    total_nfl_amb = float(h.strip().split(":")[-1])
        except (ValueError, IndexError):
            pass
        return total_fl, total_nfl, total_nfl_amb

    def __init__(self, f):
        super(AbundanceReader, self).__init__(f)
        self.comments, self.firstLine = self._read_comments_header()
        self.total_fl, self.total_nfl, self.total_nfl_amb = self.parse_comments(self.comments)

    def __iter__(self):
        if self.firstLine:
            if self.firstLine.strip() != AbundanceRecord.HEADER:
                yield AbundanceRecord.fromString(self.firstLine)
            self.firstLine = None
        for line in self.file:
            line = line.strip()
            if len(line) > 0 and line[0] != "#" and line != AbundanceRecord.HEADER:
                yield AbundanceRecord.fromString(line)


class AbundanceWriter(WriterBase):

    """
    Write comments, the header and AbundanceRecords to a file.
    """

    def __init__(self, f, comments=None,
                 total_fl=None, total_nfl=None, total_nfl_amb=None):
        super(AbundanceWriter, self).__init__(f)
        self.total_fl, self.total_nfl, self.total_nfl_amb = total_fl, total_nfl, total_nfl_amb
        self._write_comments_header(comments)

    @classmethod
    def make_comments(cls, total_fl, total_nfl, total_nfl_amb):
        """Make a comments str with total_fl, total_nfl, total_nfl_amb info."""
        return "\n".join([
            "# -----------------",
            "# Field explanation",
            "# -----------------",
            "# count_fl: Number of associated FL reads",
            "# count_nfl: Number of associated FL + unique nFL reads",
            "# count_nfl_amb: Number of associated FL + unique nFL + weighted ambiguous nFL reads",
            "# norm_fl: count_fl / total number of FL reads",
            "# norm_nfl: count_nfl / total number of FL + unique nFL reads",
            "# norm_nfl_amb: count_nfl_amb / total number of all reads",
            "%s %s" % (TOTAL_FL, total_fl),
            "%s %s" % (TOTAL_NFL, total_nfl),
            "%s %s" % (TOTAL_AMB, total_nfl_amb),
            "#"])

    def _write_comments_header(self, comments):
        """Write comments and the header."""
        c_str = None
        if comments is not None:
            if isinstance(comments, str):
                c_str = comments
            elif isinstance(comments, list) and len(comments) > 0:
                c_str = "\n".join(comments)
            else:
                raise ValueError("comments %s must be either a str or a list of str" % comments)
        elif self.total_fl and self.total_nfl and self.total_nfl_amb:
            c_str = self.make_comments(self.total_fl, self.total_nfl, self.total_nfl_amb)

        if c_str:
            self.file.write("{0}\n".format(c_str))

        self.file.write("{0}\n".format(AbundanceRecord.HEADER))

    def writeRecord(self, record):
        """Write a AbundanceRecrod."""
        if not isinstance(record, AbundanceRecord):
            raise ValueError("record type %s is not AbundanceRecord." % type(record))
        else:
            self.file.write("{0}\n".format(str(record)))
