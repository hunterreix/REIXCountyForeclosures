/*
 * intake_parse.js - Pulls the facts we need out of OCR text from a Bexar County
 * foreclosure notice, deed of trust, or loan document. Runs in the intake user's
 * browser. OCR is imperfect, so every value is only a SUGGESTION that the person
 * confirms or fixes before saving. Dates are returned as YYYY-MM-DD.
 */
(function (root) {
  var MONTHS = {jan:1,feb:2,mar:3,apr:4,may:5,jun:6,jul:7,aug:8,sep:9,sept:9,oct:10,nov:11,dec:12};
  var DATE_SRC = "(?:(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\\.?\\s+\\d{1,2}(?:st|nd|rd|th)?,?\\s+\\d{4}|\\d{1,2}\\/\\d{1,2}\\/\\d{4})";

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function toIso(s) {
    if (!s) return "";
    var m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(s.trim());
    if (m) return m[3] + "-" + pad(+m[1]) + "-" + pad(+m[2]);
    m = /^([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$/.exec(s.trim());
    if (m) {
      var mon = MONTHS[m[1].slice(0, 4).toLowerCase()] || MONTHS[m[1].slice(0, 3).toLowerCase()];
      if (mon) return m[3] + "-" + pad(mon) + "-" + pad(+m[2]);
    }
    return "";
  }

  function flat(t) { return String(t || "").replace(/[\u2018\u2019]/g, "'").replace(/\s+/g, " ").trim(); }
  function tidy(s) { return (s || "").replace(/\s+/g, " ").replace(/^[\s:,.;-]+|[\s:,;-]+$/g, "").trim(); }
  function first(re, text, group) { var m = re.exec(text); return m ? tidy(m[group || 1]) : ""; }

  // "Doc# 20261000380 09/11/2026 11:50 AM Page 1 of 3 ..." printed on every page by the Clerk
  function findStamp(text) {
    var t = flat(text);
    var m = /Doc\s*#?\s*[:.]?\s*(\d{9,12})\s+(\d{1,2}\/\d{1,2}\/\d{4})/i.exec(t) ||
            /(\d{11})\s+(\d{1,2}\/\d{1,2}\/\d{4})\s+\d{1,2}:\d{2}/.exec(t);
    return m ? { doc: m[1], date: toIso(m[2]) } : { doc: "", date: "" };
  }

  function instruments(t, exclude) {
    var re = /(?:Document|Doc\.?|Instrument|Clerk'?s\s+File)\s*(?:Number|No\.?|#)\s*[:.]?\s*(\d{9,12})/gi, out = [], m;
    while ((m = re.exec(t))) { if (m[1] !== exclude) out.push({ num: m[1], idx: m.index }); }
    return out;
  }

  function principalOf(t) {
    return first(/(?:original\s+)?principal\s+(?:sum|amount)\s+of[^$]{0,200}\$\s?([\d,]+(?:\.\d{2})?)/i, t);
  }

  function parseNotice(text) {
    var t = flat(text), stamp = findStamp(text), cands = instruments(t, stamp.doc);
    var anchor = t.search(/Type of Sale/i); if (anchor < 0) anchor = t.search(/deed of trust/i);
    var pick = null, i;
    for (i = 0; i < cands.length; i++) { if (cands[i].idx >= anchor) { pick = cands[i]; break; } }
    pick = pick || cands[0] || null;
    var win = pick ? t.slice(Math.max(0, pick.idx - 380), pick.idx + 20) : t;
    var dated = "", recorded = "", m, re;
    re = new RegExp("dated\\s+(" + DATE_SRC + ")", "gi"); while ((m = re.exec(win))) dated = m[1];
    re = new RegExp("recorded\\s+(?:on\\s+)?(" + DATE_SRC + ")", "gi"); while ((m = re.exec(win))) recorded = m[1];
    return {
      stamp_doc: stamp.doc, stamp_date: stamp.date,
      deed_instrument: pick ? pick.num : "",
      deed_recorded: toIso(recorded), deed_dated: toIso(dated),
      borrower: first(/executed\s+by\s+(.+?)(?:\s+and\s+recorded|\s+recorded|,|\.)/i, win),
      lender: first(/payable\s+to\s+the\s+order\s+of\s+(.+?)(?:\s+and\s+all\s+other|[.,]\s)/i, t) ||
              first(/Beneficiary\s*[:\-]\s*(.+?)(?:[.,;]\s|$)/i, t),
      principal: principalOf(t),
      sale_date: toIso(first(new RegExp("Date\\s*:\\s*(" + DATE_SRC + ")", "i"), t)),
      _candidates: cands.map(function (c) { return c.num; })
    };
  }

  // A "Label: value" read to the end of the line, but never past the next label
  // (some PDFs/OCR runs lose their line breaks, which would glue fields together).
  var NEXT_LABEL = /\s+(?:(?:(?:Grantors?|Borrowers?|Trustors?)(?:\(s\))?|Beneficiary|Lender|Trustee|Assignor|Assignee|Property|Principal|Maturity(?:\s+Date)?|Date|Dated)\s*[:\-]|Loan\s*(?:No\.?|Number|#))/i;
  function line(re, text) {
    var m = re.exec(text); if (!m) return "";
    return tidy(m[1].split(NEXT_LABEL)[0]).slice(0, 120);
  }

  // When the note is due in full (the "loan end date" / payoff date)
  var MATURITY = new RegExp("(?:Maturity\\s+Date|Date\\s+of\\s+Maturity|matures?\\s+on|due\\s+and\\s+payable\\s+(?:in\\s+full\\s+)?on|final\\s+(?:payment|installment)[^.]{0,80}?(?:due|payable)\\s+on)\\s*[:\\-]?\\s*(" + DATE_SRC + ")", "i");

  function parseDeedOfTrust(text) {
    var stamp = findStamp(text), t = flat(text), x = String(text || "");
    var dated = first(new RegExp("(?:dated|Date)\\s*[:\\-]?\\s*(" + DATE_SRC + ")", "i"), t);
    return {
      stamp_doc: stamp.doc, stamp_date: stamp.date, dated: toIso(dated),
      // the person who signs the deed of trust is called Grantor(s), Borrower(s) or Trustor(s), depending on the lender's form
      borrower: line(/(?:Grantors?|Borrowers?|Trustors?)(?:\(s\))?\s*[:\-]\s*([^\n]{2,100})/i, x),
      lender: line(/(?:Beneficiary|Lender)\s*[:\-]\s*([^\n]{2,100})/i, x),
      trustee: line(/Trustee\s*[:\-]\s*([^\n]{2,100})/i, x),
      loan_number: first(/Loan\s*(?:No\.?|Number|#)\s*[:.]?\s*([A-Za-z0-9][A-Za-z0-9\-\/]{3,30})/i, t),
      maturity_date: toIso(first(MATURITY, t)),
      principal: principalOf(t),
      _looksLikeDeedOfTrust: /DEED\s+OF\s+TRUST/i.test(t)
    };
  }

  function parseLoanDoc(text) {
    var stamp = findStamp(text), t = flat(text), x = String(text || "");
    var refs = instruments(t, stamp.doc);
    var subtype = /assignment\s+of/i.test(t) ? "assignment" : /release\s+of/i.test(t) ? "release" :
                  /\bpromissory\s+note\b/i.test(t) ? "note" : /\blien\b/i.test(t) ? "lien" : "";
    return {
      stamp_doc: stamp.doc, stamp_date: stamp.date,
      assignor: line(/Assignor\s*[:\-]?\s*([^\n]{2,100})/i, x),
      assignee: line(/Assignee\s*[:\-]?\s*([^\n]{2,100})/i, x),
      references: refs.length ? refs[0].num : "", principal: principalOf(t), subtype: subtype
    };
  }

  // Any other document: read the recording stamp, and guess a title from its first ALL-CAPS heading line
  function parseOtherDoc(text) {
    var stamp = findStamp(text), lines = String(text || "").split(/\n/), title = "", i, l;
    for (i = 0; i < lines.length; i++) {
      l = tidy(lines[i]);
      if (!l || /^Doc\s*#/i.test(l)) continue;
      if (/^[A-Z][A-Z0-9 \-&',.\/]{5,80}$/.test(l) && /[A-Z]{3}/.test(l)) { title = l; break; }
    }
    return { stamp_doc: stamp.doc, stamp_date: stamp.date, title: title };
  }

  var api = { toIso: toIso, findStamp: findStamp, parseNotice: parseNotice, parseDeedOfTrust: parseDeedOfTrust, parseLoanDoc: parseLoanDoc, parseOtherDoc: parseOtherDoc };
  root.IntakeParse = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
