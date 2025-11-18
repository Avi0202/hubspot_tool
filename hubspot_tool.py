from strands_agents.tools.toolkit import Toolkit
import aiohttp
import json
from datetime import datetime, timezone
from fastapi import HTTPException
from pydantic import BaseModel
from typing import Optional


class Company(BaseModel):
    name: str
    phone: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    country: Optional[str] = None
    
class Contact(BaseModel):
    name: Optional[str] = None
    email: str
    phone: Optional[str] = None

class Location(BaseModel):
    city:str
    state: Optional[str] = None
    state_code: Optional[str] = None
class Deal(BaseModel):
    pickup: Location
    delivery: Location
    quote_amount: Optional[float] = None
class HubspotDeal(BaseModel):
    company: Company
    contact: Contact
    deal: Deal

class HubSpotQuoteTool(Toolkit):
    """
    A Strands-compatible tool that automates creating a company, contact,
    deal, and follow-up email in HubSpot.
    """

    HUBSPOT_BASE_URL = "https://api.hubapi.com"
    EMAIL_GENERATION_URL = "https://admin-apis.isometrik.io/v1/agent/chat/strands/"

    def __init__(self, hubspot_token: str):
        super().__init__()
        self.token = hubspot_token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        # Register the tool method
        self.register(
            self.auto_create_quote_flow,
            name="hubspot_quote_flow",
            description="Creates/reuses a company, contact, and deal in HubSpot with a specified quote amount, then generates and attaches an email."
        )

    async def auto_create_quote_flow(self, data: HubspotDeal):
        """
        Perform the automated HubSpot quote flow.
        """
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                # ------------------------------------------------------------------
                # 1️⃣ Company creation / reuse
                # ------------------------------------------------------------------
                
                company_data = data.company.dict()
                company_name = company_data.get("name")
                if not company_name:
                    raise HTTPException(400, "Missing company name")

                print(f"\n🔹 Checking/creating company: {company_name}")
                search_payload = {
                    "filterGroups": [{
                        "filters": [{"propertyName": "name", "operator": "EQ", "value": company_name}]
                    }]
                }

                async with session.post(f"{self.HUBSPOT_BASE_URL}/crm/v3/objects/companies/search", json=search_payload) as res:
                    resp_json = await res.json()
                    results = resp_json.get("results", [])

                if results:
                    company_id = results[0]["id"]
                    print(f"✅ Found existing company: {company_name} ({company_id})")
                else:
                    create_payload = {
                        "properties": {
                            "name": company_name,
                            "phone": company_data.get("phone", ""),
                            "address": company_data.get("address_line1", ""),
                            "address2": company_data.get("address_line2", ""),
                            "city": company_data.get("city", ""),
                            "state": company_data.get("state", ""),
                            "zip": company_data.get("zip_code", ""),
                            "country": company_data.get("country", "")
                        }
                    }
                    async with session.post(f"{self.HUBSPOT_BASE_URL}/crm/v3/objects/companies", json=create_payload) as res:
                        created = await res.json()
                        company_id = created.get("id")
                        print(f"🏗️ Created new company: {company_name} ({company_id})")

                # ------------------------------------------------------------------
                # 2️⃣ Contact creation / reuse
                # ------------------------------------------------------------------
                contact_data = data.contact.dict()
                contact_email = contact_data.get("email")
                contact_name = contact_data.get("name")

                if not contact_email:
                    raise HTTPException(400, "Missing contact email")

                print(f"\n🔹 Creating contact {contact_name} ({contact_email})")

                contact_payload = {
                    "properties": {
                        "firstname": contact_name,
                        "email": contact_email,
                        "phone": contact_data.get("phone", "")
                    }
                }

                async with session.post(f"{self.HUBSPOT_BASE_URL}/crm/v3/objects/contacts", json=contact_payload) as res:
                    contact_json = await res.json()
                    contact_id = contact_json.get("id")
                    if not contact_id:
                        contact_id = contact_json.get("message", "").split("Existing ID:")[-1].strip()
                    print(f"✅ Contact ID: {contact_id}")

                # ------------------------------------------------------------------
                # 3️⃣ Deal creation
                # ------------------------------------------------------------------
                deal_data = data.deal.dict()
                pickup = deal_data.get("pickup", {})
                delivery = deal_data.get("delivery", {})
                quote_amount = deal_data.get("quote_amount", 0.0)

                deal_name = f"{contact_name or 'Customer'} Quote from {pickup.get('city','')} to {delivery.get('city','')}"
                print(f"\n🔹 Creating deal: {deal_name}")

                deal_payload = {
                    "properties": {
                        "dealname": deal_name,
                        "amount": quote_amount,
                        "dealstage": "contractsent"
                    }
                }

                async with session.post(f"{self.HUBSPOT_BASE_URL}/crm/v3/objects/deals", json=deal_payload) as res:
                    deal_json = await res.json()
                    deal_id = deal_json.get("id")
                    print(f"✅ Deal created: {deal_id}")

                # ------------------------------------------------------------------
                # 4️⃣ Associate entities
                # ------------------------------------------------------------------
                async def associate(from_type, to_type, from_id, to_id, assoc_type):
                    url = f"{self.HUBSPOT_BASE_URL}/crm/v3/associations/{from_type}/{to_type}/batch/create"
                    payload = {"inputs": [{"from": {"id": from_id}, "to": {"id": to_id}, "type": assoc_type}]}
                    async with session.post(url, json=payload) as r:
                        print(f"🔗 Assoc {from_type}->{to_type} ({assoc_type}): {r.status}")

                if company_id and deal_id:
                    await associate("companies", "deals", company_id, deal_id, "company_to_deal")

                if contact_id and deal_id:
                    await associate("contacts", "deals", contact_id, deal_id, "contact_to_deal")

                if company_id and contact_id:
                    await associate("companies", "contacts", company_id, contact_id, "company_to_contact")

                # ------------------------------------------------------------------
                # 5️⃣ Generate email
                # ------------------------------------------------------------------
                print("\n🔹 Generating email via agent …")

                payload = {
                    "session_id": "1761653686716",
                    "message": json.dumps(data),
                    "agent_id": "6900b36599417c626e85542d"
                }

                async with session.post(self.EMAIL_GENERATION_URL, json=payload) as r:
                    res_data = await r.json()
                    print(f"📨 Email generator response: {res_data}")

                text_content = (res_data.get("text") or "{}").strip()
                try:
                    parsed_email = json.loads(text_content)
                except json.JSONDecodeError:
                    parsed_email = {}

                email_props = {
                    "hs_email_direction": "EMAIL",
                    "hs_email_subject": parsed_email.get("subject", "Quote Information"),
                    "hs_email_text": parsed_email.get("body", "Please find your quote attached."),
                    "hs_timestamp": int(datetime.now(timezone.utc).timestamp() * 1000)
                }

                async with session.post(f"{self.HUBSPOT_BASE_URL}/crm/v3/objects/emails", json={"properties": email_props}) as res:
                    email_json = await res.json()
                    email_id = email_json.get("id")
                    print(f"✅ Created email {email_id}")

                if email_id and deal_id:
                    await associate("emails", "deals", email_id, deal_id, "email_to_deal")

                print("\n🎯 HubSpot automation flow completed successfully!")

                return {
                    "company_id": company_id,
                    "contact_id": contact_id,
                    "deal_id": deal_id,
                    "email_id": email_id,
                    "quote_amount": quote_amount
                }

        except Exception as e:
            print(f"❌ FAILED HubSpot auto flow: {e}")
            raise HTTPException(status_code=500, detail=str(e))
