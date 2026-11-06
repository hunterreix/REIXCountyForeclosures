const P = require(__dirname + '/../static/intake_parse.js');
let fails = 0;
const eq = (label, got, want) => { const ok = got === want; if (!ok) fails++; console.log((ok ? "PASS " : "FAIL ") + label + (ok ? "" : `  got=${JSON.stringify(got)} want=${JSON.stringify(want)}`)); };

// Text as read from the real notice 20261000380 (page header stamp + section 5 + section 6)
const notice = `Doc# 20261000380 09/11/2026 11:50 AM Page 2 of 3 Lucy Adame-Clark, Bexar County Clerk
NOTICE OF FORECLOSURE SALE
Notice is hereby given of a public nonjudicial foreclosure sale.
1. Property To Be Sold. The property to be sold is described as follows: All the real property described in the below described recorded Deed of Trust, including but not necessarily limited to: Lot 5 and 6, New City Block 7482
2. Date, Time, and Place of Sale. The sale is scheduled to be held at the following date, time, and place:
Date: October 6, 2026
Time: The sale shall begin no earlier than 10:00 A.M.
5. Type of Sale. The sale is a nonjudicial deed of trust lien and security interest foreclosure sale being conducted pursuant to the power of sale granted by the Deed of Trust ("Deed of Trust"), dated May 24, 2019 executed by NES SAYAG and recorded on May 31, 2019, under Document Number: 20190102655 in the Official Public Records of Bexar County, Texas. The sale is "As Is", and pursuant to the Texas Property Code § 51.009.
6. Obligations Secured. The deeds of trust provide that it secures the payment of the indebtedness and obligations therein described (collectively the "Obligations") including but not limited to the promissory note in the original principal amount of TWO HUNDRED AND FORTY-THREE THOUSAND SEVEN HUNDRED AND FIFTY AND NO/100THS DOLLARS ($243,750.00) executed by NES SAYAG, and payable to the order of First United Bank and Trust Co. and all other sums of indebtedness permitted by the deed of trust.`;
let n = P.parseNotice(notice);
eq("notice: stamp doc", n.stamp_doc, "20261000380");
eq("notice: stamp date", n.stamp_date, "2026-09-11");
eq("notice: deed instrument", n.deed_instrument, "20190102655");
eq("notice: deed dated", n.deed_dated, "2019-05-24");
eq("notice: deed recorded", n.deed_recorded, "2019-05-31");
eq("notice: borrower", n.borrower, "NES SAYAG");
eq("notice: lender", n.lender, "First United Bank and Trust Co");
eq("notice: principal", n.principal, "243,750.00");
eq("notice: sale date", n.sale_date, "2026-10-06");

// Variant wording and OCR-ish noise
let v = P.parseNotice(`Doc # 20261100057 10/20/2026 9:05 AM Page 1 of 2
Type of Sale. pursuant to the Deed of Trust dated 3/15/2021, executed by JOHN Q PUBLIC, and recorded on 3/22/2021 under Clerk's File No. 20210055512 in the Official Public Records of Real Property of Bexar County.`);
eq("variant: instrument via Clerk's File No.", v.deed_instrument, "20210055512");
eq("variant: numeric dates", v.deed_recorded + "|" + v.deed_dated, "2021-03-22|2021-03-15");
eq("variant: borrower", v.borrower, "JOHN Q PUBLIC");
eq("variant: the notice's own stamp number is never picked as the deed", v.deed_instrument === v.stamp_doc, false);
let e = P.parseNotice("garbled text with nothing useful");
eq("no match returns blanks, not errors", e.deed_instrument + e.borrower + e.stamp_doc, "");

// Deed of trust
let d = P.parseDeedOfTrust(`Doc# 20190102655 05/31/2019 2:14 PM Page 1 of 18 Lucy Adame-Clark, Bexar County Clerk
DEED OF TRUST
Date: May 24, 2019
Grantor: NES SAYAG, a single man
Beneficiary: First United Bank and Trust Co.
Trustee: Dwight Robinson
in the principal amount of FIFTY THOUSAND DOLLARS ($243,750.00)`);
eq("deed: stamp", d.stamp_doc + "|" + d.stamp_date, "20190102655|2019-05-31");
eq("deed: dated", d.dated, "2019-05-24");
eq("deed: grantor", d.borrower, "NES SAYAG, a single man");
eq("deed: beneficiary", d.lender, "First United Bank and Trust Co.");
eq("deed: trustee", d.trustee, "Dwight Robinson");
eq("deed: looks like a deed of trust", d._looksLikeDeedOfTrust, true);

// Loan document (assignment)
let l = P.parseLoanDoc(`Doc# 20200011111 02/02/2020 10:00 AM Page 1 of 3 Lucy Adame-Clark, Bexar County Clerk
ASSIGNMENT OF DEED OF TRUST
Assignor: First United Bank and Trust Co.
Assignee: Lakeview Loan Servicing
the Deed of Trust recorded under Document Number: 20190102655`);
eq("loan: subtype", l.subtype, "assignment");
eq("loan: assignor/assignee", l.assignor + "|" + l.assignee, "First United Bank and Trust Co.|Lakeview Loan Servicing");
eq("loan: referenced deed of trust", l.references, "20190102655");
eq("loan: stamp not used as the reference", l.references === l.stamp_doc, false);

eq("date helper", P.toIso("Sept. 3rd, 2019") + "|" + P.toIso("12/1/2020"), "2019-09-03|2020-12-01");
console.log(fails ? `\n${fails} FAILED` : "\nALL PARSER TESTS PASSED"); process.exit(fails ? 1 : 0);
// same deed of trust with NO line breaks at all (text layer glued together)
{
  const P2 = require(__dirname + '/../static/intake_parse.js');
  const flat = P2.parseDeedOfTrust("Doc# 20190102655 05/31/2019 2:14 PM Page 1 of 18 DEED OF TRUST Date: May 24, 2019 Grantor: NES SAYAG, a single man Beneficiary: First United Bank and Trust Co. Trustee: Dwight Robinson Property: Lot 5");
  console.log((flat.borrower==="NES SAYAG, a single man"?"PASS ":"FAIL ")+"glued text: grantor stops at next label -> "+JSON.stringify(flat.borrower));
  console.log((flat.lender==="First United Bank and Trust Co."?"PASS ":"FAIL ")+"glued text: beneficiary -> "+JSON.stringify(flat.lender));
  console.log((flat.trustee==="Dwight Robinson"?"PASS ":"FAIL ")+"glued text: trustee -> "+JSON.stringify(flat.trustee));
}
