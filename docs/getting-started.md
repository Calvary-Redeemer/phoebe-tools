# Getting Started

> Source: https://api.planningcenteronline.com/docs/overview/getting-started

Planning Center provides a REST API for developers to interact with a Planning Center account. Our API follows the [JSON-API](https://api.planningcenteronline.com/docs/overview/json-api) spec.

## User Account

All requests to our API are made on behalf of a user. As such, in order to make requests to our API you will need a **user account**. The type of integration you are making will determine which type of user account you should use.

If you are building an integration or tool for a single church, you can use a **Personal Access Token** on a user within that organization. If you're building a third-party integration for a product or service, we suggest that you [create a new organization](https://accounts.planningcenteronline.com/) and create your OAuth Application on this account. You can access the full API on the free subscription tiers, and also add sample data for you to work with.

From your user account, you can access your [developer account](https://api.planningcenteronline.com/oauth/applications). In your developer account, you can create new applications using OAuth, generate Personal Access Tokens, configure webhooks, and play with the API Explorer using real organizational data. You can read more about [API authentication](https://api.planningcenteronline.com/docs/overview/authentication).

**Important:** Your integration will act under the [permission levels](https://help.planningcenter.com/en/136861-permissions-in-people.html) your user account has.

## Staying Up-to-date

We try very hard not to introduce breaking changes to the API, but occasionally we need to make fundamental modifications that may require adjustments on your part. When that happens, we send an email to the API News mailing list, to which you can [sign up here](http://eepurl.com/jempmE).
